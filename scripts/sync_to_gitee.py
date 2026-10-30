#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据源 → Gitee 同步脚本

每日直接从数据源拉取最新文件，通过 Gitee OpenAPI 推送至自己的 Gitee 仓库：
  1. fear-greed.csv  —— CNN API 直抓（2021-02-01 至今）+ 本地 pre-2021 静态归档合并
  2. vix_history.csv —— VIX 收盘价：Yahoo ^VIX（收盘后即时可得）为主，
                        CBOE 官方全量历史覆盖/兜底，Stooq 第三级，
                        统一输出 date,close（与聚宽端本地缓存同格式）

不再依赖上游 GitHub 仓库 whit3rabbit/fear-greed-data 的更新时机，
避免其 Actions 排队延迟导致的数据滞后。

前提：仓库需包含 datasets/archive/fear-greed-pre-2021.csv（fork 上游后自带）。
"""

import base64
import csv
import io
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ============ 配置（可用环境变量覆盖） ============
GITEE_TOKEN = os.environ['GITEE_TOKEN']                      # Gitee 私人令牌（仅需 projects 权限）
GITEE_REPO = os.environ.get('GITEE_REPO', 'vtnt0592/fear-greed-data')
GITEE_BRANCH = os.environ.get('GITEE_BRANCH', 'main')

GITEE_API = 'https://gitee.com/api/v5/repos/%s/contents/{path}' % GITEE_REPO
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                         'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

# CNN 恐惧贪婪指数 API（返回 2021-02-01 至今全量历史，一次 GET）
CNN_FG_URL = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2021-02-01'
CNN_HEADERS = {
    'User-Agent': os.environ.get('CNN_FETCH_USER_AGENT') or HEADERS['User-Agent'],
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.cnn.com',
    'Referer': 'https://www.cnn.com/',
}

# pre-2021 静态归档（fork 仓库自带，actions/checkout 后本地可读）
ARCHIVE_PATH = 'datasets/archive/fear-greed-pre-2021.csv'
ARCHIVE_CUTOFF = '2021-02-01'

# 美股收盘缓冲：常规收盘 16:00 ET = 20:00 UTC（夏令时）/ 21:00 UTC（冬令时）。
# 取 22:00 UTC 作统一缓冲，早于此时间抓取时，当天（UTC 日期）的数据点一律不入库，
# 避免把盘中半成品写进文件（对齐上游 whit3rabbit 只发布已收盘交易日的效果）。
US_CLOSE_BUFFER_UTC = (22, 0)

# CBOE VIX 全量历史（官方口径，但文件更新时点偏晚，且部分区域被 Cloudflare 拦）
CBOE_VIX_URL = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
# Yahoo ^VIX 日线（美股收盘后几分钟即可拿到当日收盘，适合补 CBOE 的更新延迟）
YAHOO_VIX_HOSTS = ['query1.finance.yahoo.com', 'query2.finance.yahoo.com']
# Stooq VIX 日线 CSV（第三级兜底）
STOOQ_VIX_URL = 'https://stooq.com/q/d/l/?s=%5Evix&i=d'


# ============ CNN 恐惧贪婪指数 ============
def fetch_cnn_fear_greed():
    """从 CNN API 抓取恐惧贪婪指数（2021-02-01 至今）。
    返回 list[tuple(date_str, score, rating)]，按日期升序（同一日期可能有多条，
    后者为当日更新的快照，写入时覆盖前者 = 上游 build_combined.py 的 keep='last'）。

    与上游 whit3rabbit 一致的取数语义：
      1. 每个日期取其最后一个点（美股收盘后运行时，实时点即当日收盘值）；
      2. 只发布已收盘的交易日——抓取时刻早于 US_CLOSE_BUFFER_UTC 时，
         当天（UTC 日期）的点一律丢弃，避免写入盘中半成品；
      3. 晚于当前 UTC 日期的未来点一律丢弃。"""
    resp = requests.get(CNN_FG_URL, headers=CNN_HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    hist = payload.get('fear_and_greed_historical') or {}
    data = hist.get('data') or []
    if not data:
        raise RuntimeError('CNN 响应缺少 fear_and_greed_historical.data')

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    market_closed = (now.hour, now.minute) >= US_CLOSE_BUFFER_UTC

    rows = []
    dropped = 0
    for entry in data:
        ts_ms = entry['x']
        t = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        d = t.date().isoformat()
        if d > today or (d == today and not market_closed):
            dropped += 1          # 未来点 / 当天尚未收盘
            continue
        rows.append((d, float(entry['y']), entry.get('rating', '')))
    if not rows:
        raise RuntimeError('CNN 无可用数据点（当天尚未收盘？）')
    # 稳定排序：仅按日期分组，同日保持 CNN 原始顺序（日线点在前、实时点在后）
    rows.sort(key=lambda r: r[0])
    merged = len(rows) - len(set(r[0] for r in rows))
    print('  CNN 直抓: %d 个日期 (%s ~ %s)，丢弃未收盘/未来点 %d 个，同日期多快照合并 %d 个'
          % (len(set(r[0] for r in rows)), rows[0][0], rows[-1][0], dropped, merged))
    return rows


def _rating_for(score):
    if score < 25:
        return 'extreme fear'
    if score < 45:
        return 'fear'
    if score < 55:
        return 'neutral'
    if score < 75:
        return 'greed'
    return 'extreme greed'


def load_pre2021_archive():
    """读取本地 pre-2021 静态归档（fork 仓库自带）。
    返回 list[tuple(date_str, score_float, rating_str)]。"""
    if not os.path.exists(ARCHIVE_PATH):
        raise FileNotFoundError('缺少归档文件: %s（需 fork whit3rabbit/fear-greed-data）' % ARCHIVE_PATH)
    rows = []
    with open(ARCHIVE_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get('Date', '')
            if d < ARCHIVE_CUTOFF:
                score = float(row['Fear Greed'])
                rating = row.get('Rating', '') or _rating_for(score)
                rows.append((d, score, rating))
    print('  pre-2021 归档: %d 行 (%s ~ %s)' % (len(rows), rows[0][0], rows[-1][0]))
    return rows


def build_fear_greed_csv():
    """构建完整 fear-greed.csv：pre-2021 归档 + CNN 直抓 2021+ 数据。
    返回 bytes (CSV 内容)。"""
    archive_rows = load_pre2021_archive()
    cnn_rows = fetch_cnn_fear_greed()

    # 合并：CNN 数据覆盖归档中的重叠日期；同日期多条 CNN 快照时后者覆盖前者
    # （对应上游 build_combined.py 的 drop_duplicates(keep='last')）
    combined = {}
    for d, score, rating in archive_rows:
        combined[d] = (score, rating)
    for d, score, rating in cnn_rows:
        combined[d] = (score, rating)

    sorted_dates = sorted(combined.keys())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Date', 'Fear Greed', 'Rating'])
    for d in sorted_dates:
        score, rating = combined[d]
        w.writerow([d, score, rating])

    content = buf.getvalue().encode('utf-8')
    print('  合并完成: %d 行 (%s ~ %s)' % (len(sorted_dates), sorted_dates[0], sorted_dates[-1]))
    return content


# ============ 通用下载 ============
def download(url):
    """下载上游文件，返回 bytes；失败抛异常"""
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    content = r.content
    if len(content) < 1000:
        raise ValueError('内容过短（%d bytes），疑似上游异常: %s' % (len(content), url))
    return content


# ============ VIX（多源合并，解决官方文件更新偏晚） ============
def _norm_date(s):
    """统一日期格式为 YYYY-MM-DD（兼容 CBOE 的 MM/DD/YYYY 与 ISO 格式）"""
    s = str(s).strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError('无法识别的日期: %s' % s)


def fetch_cboe_vix():
    """CBOE 官方 VIX_History.csv → list[(date, close)]。更新偏晚，作为官方口径兜底。"""
    text = download(CBOE_VIX_URL).decode('utf-8-sig')
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        d = row.get('DATE') or row.get('date')
        c = row.get('CLOSE') or row.get('close')
        if not d or not c:
            continue
        try:
            rows.append((_norm_date(d), float(c)))
        except (ValueError, TypeError):
            continue
    if not rows:
        raise RuntimeError('CBOE 文件解析为空')
    rows.sort()
    print('  CBOE VIX_History: %d 行 (%s ~ %s)' % (len(rows), rows[0][0], rows[-1][0]))
    return rows


def fetch_yahoo_vix(rng='max'):
    """Yahoo ^VIX 日线 → list[(date, close)]。美股收盘后几分钟即可拿到当日收盘，
    是解决 CBOE 文件 T+1 更新延迟的主源。range='max' 返回 1990 年至今全量。"""
    last_err = None
    for host in YAHOO_VIX_HOSTS:
        for _ in range(2):
            try:
                url = ('https://' + host + '/v8/finance/chart/%5EVIX?interval=1d&range=' + rng)
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                result = (r.json().get('chart') or {}).get('result') or []
                if not result:
                    raise RuntimeError('Yahoo 响应缺少 chart.result')
                ts = result[0].get('timestamp') or []
                closes = ((result[0].get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
                rows = []
                for t, c in zip(ts, closes):
                    if c is None:
                        continue
                    rows.append((datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(),
                                 float(c)))
                if not rows:
                    raise RuntimeError('Yahoo 解析后为空')
                rows.sort()
                print('  Yahoo ^VIX(%s): %d 行 (%s ~ %s)'
                      % (host, len(rows), rows[0][0], rows[-1][0]))
                return rows
            except Exception as e:
                last_err = e
                time.sleep(2)
    raise RuntimeError('Yahoo 全部失败: %s' % last_err)


def fetch_stooq_vix():
    """Stooq VIX 日线 → list[(date, close)]，第三级兜底。"""
    text = download(STOOQ_VIX_URL).decode('utf-8-sig')
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        d, c = row.get('Date'), row.get('Close')
        if not d or not c:
            continue
        try:
            rows.append((_norm_date(d), float(c)))
        except (ValueError, TypeError):
            continue
    if not rows:
        raise RuntimeError('Stooq 解析为空')
    rows.sort()
    print('  Stooq VIX: %d 行 (%s ~ %s)' % (len(rows), rows[0][0], rows[-1][0]))
    return rows


def build_vix_csv():
    """多源合并构建 vix_history.csv（date,close）。
    优先级：Yahoo 最新 → CBOE 官方覆盖重叠日期（口径最准）→ Stooq 仅补空缺。
    任一源失败不影响其余，全部失败才抛异常。"""
    merged = {}
    errors = []

    def _try(name, fn, overwrite):
        try:
            rows = fn()
        except Exception as e:
            errors.append('%s: %s' % (name, e))
            print('  [WARN] %s 获取失败: %s' % (name, e))
            return
        for d, c in rows:
            if overwrite:
                merged[d] = c
            else:
                merged.setdefault(d, c)

    _try('Yahoo', fetch_yahoo_vix, overwrite=True)     # 最快，先铺全量
    _try('CBOE', fetch_cboe_vix, overwrite=True)       # 官方口径覆盖重叠日
    _try('Stooq', fetch_stooq_vix, overwrite=False)    # 只补前两源缺失的日期

    if not merged:
        raise RuntimeError('全部 VIX 数据源失败 → %s' % '; '.join(errors))

    dates = sorted(merged.keys())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['date', 'close'])
    for d in dates:
        w.writerow([d, '%.2f' % merged[d]])
    content = buf.getvalue().encode('utf-8')
    print('  VIX 合并完成: %d 行 (%s ~ %s)' % (len(dates), dates[0], dates[-1]))
    return content


# ============ Gitee 推送 ============
def gitee_get_sha(path):
    """读取 Gitee 上已有文件的 sha 和内容；文件不存在返回 (None, None)"""
    r = requests.get(GITEE_API.format(path=path),
                     params={'ref': GITEE_BRANCH}, timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    remote_b64 = (data.get('content') or '').replace('\n', '')
    return data.get('sha'), remote_b64


def gitee_push(path, content_bytes):
    """推送单个文件到 Gitee；内容未变化则跳过。返回状态字符串"""
    sha, remote_b64 = gitee_get_sha(path)
    local_b64 = base64.b64encode(content_bytes).decode('ascii')

    if remote_b64 is not None and remote_b64 == local_b64:
        return 'UNCHANGED'

    payload = {
        'access_token': GITEE_TOKEN,
        'content': local_b64,
        'message': 'sync %s' % path,
        'branch': GITEE_BRANCH,
    }
    if sha:
        payload['sha'] = sha
        r = requests.put(GITEE_API.format(path=path), json=payload, timeout=60)
    else:
        r = requests.post(GITEE_API.format(path=path), json=payload, timeout=60)
    r.raise_for_status()
    return 'UPDATED' if sha else 'CREATED'


def main():
    ok = True

    # 1. fear-greed.csv —— CNN API 直抓 + pre-2021 归档合并
    try:
        content = build_fear_greed_csv()
        with open('fear-greed.csv', 'wb') as f:
            f.write(content)
        status = gitee_push('fear-greed.csv', content)
        print('[%s] fear-greed.csv ← CNN API (%d bytes)' % (status, len(content)))
    except Exception as e:
        ok = False
        print('[FAILED] fear-greed.csv: %s' % e)

    # 2. vix_history.csv —— Yahoo(主) + CBOE(官方兜底) + Stooq 合并
    try:
        content = build_vix_csv()
        with open('vix_history.csv', 'wb') as f:
            f.write(content)
        status = gitee_push('vix_history.csv', content)
        print('[%s] vix_history.csv ← VIX 多源合并 (%d bytes)' % (status, len(content)))
    except Exception as e:
        ok = False
        print('[FAILED] vix_history.csv: %s' % e)

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
