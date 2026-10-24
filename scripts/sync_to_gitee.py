#!/usr/bin/env python3
"""把上游 fear-greed.csv 与 CBOE VIX 历史同步到 Gitee。

GitHub Actions 使用：配置 secret GITEE_TOKEN
本地调试：GITEE_TOKEN=xxx python scripts/sync_to_gitee.py
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

OWNER = 'vtnt0592'
REPO = 'fear-greed-data'
BRANCH = 'main'
TOKEN = os.environ.get('GITEE_TOKEN', '').strip()

FEAR_URL = 'https://raw.githubusercontent.com/whit3rabbit/fear-greed-data/main/fear-greed.csv'
VIX_URL = 'https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()

def normalize_vix(raw):
    """CBOE 原始 DATE,OPEN,HIGH,LOW,CLOSE → date,close（升序）"""
    rows = []
    for line in raw.decode('utf-8-sig').strip().splitlines()[1:]:
        parts = line.split(',')
        if len(parts) < 5:
            continue
        try:
            m, d, y = parts[0].split('/')
            rows.append(('%s-%s-%s' % (y, m, d), parts[4]))
        except ValueError:
            continue
    rows.sort()
    return ('date,close\n' + '\n'.join('%s,%s' % r for r in rows) + '\n').encode()

def api(path):
    return 'https://gitee.com/api/v5/repos/%s/%s/contents/%s' % (OWNER, REPO, path)

def get_remote(path):
    url = '%s?ref=%s&access_token=%s' % (api(path), BRANCH, TOKEN)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def put_remote(path, content, sha):
    payload = {
        'access_token': TOKEN,
        'content': base64.b64encode(content).decode(),
        'message': 'data: auto sync %s' % datetime.utcnow().strftime('%Y-%m-%d'),
        'branch': BRANCH,
    }
    if sha:
        payload['sha'] = sha
    req = urllib.request.Request(
        api(path), data=json.dumps(payload).encode(),
        method='PUT' if sha else 'POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def main():
    if not TOKEN:
        print('ERROR: 缺少 GITEE_TOKEN')
        return 1
    items = [
        ('fear-greed.csv', fetch(FEAR_URL)),
        ('vix_history.csv', normalize_vix(fetch(VIX_URL))),
    ]
    changed = 0
    for name, content in items:
        with open(name, 'wb') as f:
            f.write(content)
        remote = get_remote(name)
        old = ''
        if remote and remote.get('content'):
            old = base64.b64decode(remote['content']).decode('utf-8-sig', 'ignore')
        if old.strip() == content.decode('utf-8-sig').strip():
            print('%s: 无变化，跳过' % name)
            continue
        put_remote(name, content, remote.get('sha') if remote else None)
        changed += 1
        print('%s: 已更新' % name)
    print('完成，更新 %d 个文件' % changed)
    return 0

if __name__ == '__main__':
    sys.exit(main())
