#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import argparse
import requests
from datetime import datetime, timedelta
import subprocess
from typing import List, Set
import json

# 添加项目根目录到 Python 路径
PATH = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.append(PATH)

from utils import http_get
from push import PushToGist
from logger import logger

from datetime import datetime, timedelta, timezone
import requests

def get_gist_history(gist_id: str, token: str, filename: str, hours: int = 6) -> List[str]:
    """获取 gist 历史文件内容"""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}"
    }
    
    # 获取 gist 历史记录
    url = f"https://api.github.com/gists/{gist_id}/commits?per_page=100"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        logger.error(f"Failed to get gist history: {response.status_code}")
        return []
    
    history = response.json()
    now = datetime.now()
    recent_versions = []
    
    # 筛选最近几小时的版本
    for version in history:
        version_time = datetime.strptime(version["committed_at"], "%Y-%m-%dT%H:%M:%SZ")
        if now - version_time <= timedelta(hours=hours):
            recent_versions.append(version["version"])
    
    # 获取每个版本的文件内容
    contents = set()
    for version in recent_versions:
        url = f"https://api.github.com/gists/{gist_id}/{version}"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if filename in data["files"]:
                content = data["files"][filename]["content"]
                # 提取订阅链接
                links = [line.strip() for line in content.split("\n") if line.strip() and not line.startswith("#")]
                contents.update(links)
    
    return list(contents)

def upload(token, gist_id, content, filename):
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Python/3.x",
        "Authorization": f"token {token}",
    }

    """上传文件到 Gist"""
    
    data = {
        "files": {
            filename: {
                "content": content
            }
        }
    }
    
    try:
        response = requests.patch(f"https://api.github.com/gists/{gist_id}", headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        return False
        

def convert_subscription(links: List[str], output_file: str) -> bool:
    """使用 subconverter 转换订阅"""
    # 创建临时文件存储订阅链接
    temp_file = os.path.join(PATH, "data", "temp_subscribes.txt")
    os.makedirs(os.path.dirname(temp_file), exist_ok=True)
    
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write("\n".join(links))
    
    # 获取 subconverter 可执行文件路径
    subconverter_bin = os.path.join(PATH, "subconverter", "subconverter-darwin-amd")
    if not os.path.exists(subconverter_bin):
        logger.error("Subconverter binary not found")
        return False
    
    # 执行转换
    try:
        subprocess.run([
            subconverter_bin,
            "-g",
            "-u", temp_file,
            "-o", output_file
        ], check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to convert subscription: {e}")
        return False
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

def main():
    parser = argparse.ArgumentParser(description="Merge and convert subscriptions")
    parser.add_argument("--gist-id", required=True, help="Gist ID")
    parser.add_argument("--token", required=True, help="GitHub token")
    parser.add_argument("--filename", default="subscribes.txt", help="Source filename in gist")
    parser.add_argument("--hours", type=int, default=6, help="Hours to look back")
    parser.add_argument("--output", default="merged_subscribes.txt", help="Output filename")
    
    args = parser.parse_args()
    scan = []
    with open(os.path.join("data", args.filename), "r", encoding="utf-8") as f:
        scan = f.readlines()
    if scan and len(scan) > 0:
        upload(args.token, args.gist_id, '\n'.join(scan), args.filename)

    # 获取历史订阅
    logger.info("Fetching gist history...")
    links = get_gist_history(args.gist_id, args.token, args.filename, args.hours)
    logger.info(f"Found {len(links)} unique subscription links")
    
    if not links:
        logger.error("No subscription links found")
        return
    # with open(args.output, 'w', encoding='utf-8') as f:
    #     f.write()
    # 转换订阅
    # output_file = os.path.join(PATH, "data", args.output)
    # logger.info("Converting subscriptions...")
    # if not convert_subscription(links, output_file):
    #     logger.error("Failed to convert subscriptions")
    #     return
    
    # 上传到 gist
    logger.info("Uploading to gist...")

    upload(args.token, args.gist_id, '\n'.join(links), args.output)

    # push_tool = PushToGist(token=args.token)
    # if not push_tool.upload(filepath=args.output, gist_id=args.gist_id):
    #     logger.error("Failed to upload to gist")
    #     return
    
    logger.info("Done!")

if __name__ == "__main__":
    main() 
