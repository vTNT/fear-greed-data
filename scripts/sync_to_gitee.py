#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据源 → Gitee 同步脚本

每日直接从数据源拉取最新文件，通过 Gitee OpenAPI 推送至自己的 Gitee 仓库：
  1. fear-greed.csv  —— CNN API 直抓（2021-02-01 至今）+ 本地 pre-2021 静态归档合并
  2. vix_history.csv —— CBOE 官方 VIX 全量历史

不再依赖上游 GitHub 仓库 whit3rabbit/fear-greed-data 的更新时机，
避免其 Actions 排队延迟导致的数据滞后。

前提：仓库需包含 datasets/archive/fear-greed-pre-2021.csv（fork 上游后自带）。
"""

import base64
import csv
import io
import os
import sys
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

# CBOE VIX 全量历史
CBOE_VIX_URL = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'


# ============ CNN 恐惧贪婪指数 ============
def fetch_cnn_fear_greed():
    """从 CNN API 抓取恐惧贪婪指数（2021-02-01 至今）。
    返回 list[tuple(date_str, score, rating)]，按日期升序。"""
    resp = requests.get(CNN_FG_URL, headers=CNN_HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    hist = payload.get('fear_and_greed_historical') or {}
    data = hist.get('data') or []
    if not data:
        raise RuntimeError('CNN 响应缺少 fear_and_greed_historical.data')

    rows = []
    for entry in data:
        ts_ms = entry['x']
        score = float(entry['y'])
        rating = entry.get('rating', '')
        d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        rows.append((d.isoformat(), score, rating))
    rows.sort(key=lambda r: r[0])
    print('  CNN 直抓: %d 行 (%s ~ %s)' % (len(rows), rows[0][0], rows[-1][0]))
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

    # 合并：CNN 数据覆盖归档中的重叠日期
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

    # 2. vix_history.csv —— CBOE 直连
    try:
        content = download(CBOE_VIX_URL)
        with open('vix_history.csv', 'wb') as f:
            f.write(content)
        status = gitee_push('vix_history.csv', content)
        print('[%s] vix_history.csv ← CBOE (%d bytes)' % (status, len(content)))
    except Exception as e:
        ok = False
        print('[FAILED] vix_history.csv: %s' % e)

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
