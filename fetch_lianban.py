# -*- coding: utf-8 -*-
"""
连板天梯 · 数据获取脚本
======================
使用 akshare 获取 A 股当日涨停（连板）股池数据，处理成标准 JSON，
保存为 data.json 并打印到控制台，供前端 HTML 直接读取或手动粘贴使用。

用法：
    python fetch_lianban.py            # 默认取今天，若当天无数据自动回退最近交易日
    python fetch_lianban.py 20261021   # 指定日期，格式 YYYYMMDD

依赖：
    pip install akshare
"""
import sys
import json
import datetime as dt

try:
    sys.stdout.reconfigure(encoding='utf-8')  # 防止 Windows 控制台 GBK 打印中文报错
except Exception:
    pass

try:
    import akshare as ak
    import pandas as pd
except ImportError:
    print('缺少依赖：请先执行  pip install akshare  后重试', file=sys.stderr)
    sys.exit(1)

WEEKDAYS = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']


def fetch_pool(date_str):
    """获取指定日期的涨停股池（东方财富源），失败或为空返回 None。"""
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        if df is None or len(df) == 0:
            return None
        return df
    except Exception as exc:
        print(f'[警告] {date_str} 数据获取失败：{exc}', file=sys.stderr)
        return None


def normalize_time(val):
    """把封板时间统一成 HH:MM，兼容 '09:25:00' / '092500' / '09:25' 等格式；空值返回 '—'。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return '—'
    s = str(val).strip()
    if s in ('', 'nan', 'NaT', 'None'):
        return '—'
    s = s.replace(':', '').replace('-', '')   # 去掉冒号，统一成 092500
    if len(s) < 6:
        s = s.zfill(6)
    if len(s) >= 4 and s[:4].isdigit():
        return s[:2] + ':' + s[2:4]
    return '—'


def enrich_industry(df):
    """尽力用东财行业板块全名修正被截断的行业名；接口不可用则保留原值。"""
    try:
        names = ak.stock_board_industry_name_em()['板块名称'].astype(str).tolist()
    except Exception:
        return df

    def fix(name):
        n = str(name)
        if len(n) < 4:
            return n
        for full in names:
            if full.startswith(n) or n in full:
                return full
        return n

    df['industry'] = df['industry'].apply(fix)
    return df


def main():
    # ---------- 1. 目标日期 ----------
    if len(sys.argv) > 1:
        target = sys.argv[1]
        try:
            dt.datetime.strptime(target, '%Y%m%d')
        except ValueError:
            print('日期格式错误：应为 YYYYMMDD，例如 20261021', file=sys.stderr)
            sys.exit(1)
    else:
        target = dt.date.today().strftime('%Y%m%d')

    # ---------- 2. 拉取数据，当天拿不到则向前回退最近交易日 ----------
    df = fetch_pool(target)
    used_date = target
    if df is None:
        d = dt.datetime.strptime(target, '%Y%m%d').date()
        for _ in range(10):
            d -= dt.timedelta(days=1)
            if d.weekday() >= 5:          # 跳过周末
                continue
            cand = d.strftime('%Y%m%d')
            df = fetch_pool(cand)
            if df is not None:
                used_date = cand
                print(f'[提示] {target} 暂无数据，已回退到最近交易日 {cand}', file=sys.stderr)
                break
    if df is None:
        print('获取失败：近 10 个交易日均无法获取数据，请检查网络后重试。', file=sys.stderr)
        sys.exit(1)

    # ---------- 3. 字段标准化（兼容不同 akshare 版本列名） ----------
    df = df.rename(columns={
        '代码': 'code', '名称': 'name', '连板数': 'height',
        '首次封板时间': 'time', '所属行业': 'industry', '所属概念': 'industry',
    })
    if 'industry' not in df.columns:
        df['industry'] = '—'
    if 'time' not in df.columns:
        df['time'] = '—'
    if 'code' not in df.columns or 'name' not in df.columns:
        print('获取失败：涨停股池字段结构与预期不符，请升级 akshare 后重试。', file=sys.stderr)
        sys.exit(1)

    # ---------- 4. 清洗：剔除 ST / 退市整理期，规范化字段 ----------
    mask = ~df['name'].astype(str).str.contains('ST|退', na=False)
    df = df[mask].copy()
    df['time'] = df['time'].apply(normalize_time)
    df['height'] = pd.to_numeric(df['height'], errors='coerce').fillna(1).astype(int)
    df['industry'] = df['industry'].astype(str).replace({'nan': '—', 'NaT': '—'}).fillna('—')
    df = enrich_industry(df)                # 修正可能被截断的行业名（失败则保留原值）
    df['yizi'] = df['time'] == '09:25'   # 首次封板 09:25 视为一字板

    # ---------- 5. 排序：连板高度从高到低，同高度按封板时间从早到晚 ----------
    df = df.sort_values(['height', 'time'], ascending=[False, True]).reset_index(drop=True)

    # ---------- 6. 组装个股 JSON ----------
    stocks = [
        {
            'code': str(r['code']),
            'name': str(r['name']),
            'height': int(r['height']),
            'first_seal_time': str(r['time']),
            'industry': str(r['industry']),
            'yizi': bool(r['yizi']),
        }
        for _, r in df.iterrows()
    ]

    # ---------- 7. 板块统计：按所属行业统计当日涨停家数（取前 10） ----------
    sector_stats = (
        df.groupby('industry')['code'].count()
        .sort_values(ascending=False)
        .head(10)
        .reset_index()
    )
    sector_stats.columns = ['industry', 'count']

    d = dt.datetime.strptime(used_date, '%Y%m%d')
    result = {
        'date': used_date,
        'date_display': f'{d.year}-{d.month:02d}-{d.day:02d} {WEEKDAYS[d.weekday()]}',
        'update_time': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total': len(stocks),
        'max_height': int(df['height'].max()) if len(df) else 0,
        'stocks': stocks,
        'sector_stats': sector_stats.to_dict(orient='records'),
    }

    # ---------- 8. 保存 data.json 并打印 ----------
    out = json.dumps(result, ensure_ascii=False, indent=2)
    with open('data.json', 'w', encoding='utf-8') as f:
        f.write(out)
    print(out)


if __name__ == '__main__':
    main()
