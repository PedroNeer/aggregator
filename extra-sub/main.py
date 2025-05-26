#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import requests
from datetime import datetime, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.logger import logger
from utils.subscription_validator import is_valid_subscription_content
from services.gist_service import GistService
from services.telegram_service import TelegramService
from config import CONFIG


def fetch_nodes(url):
    """验证订阅链接是否可访问且内容有效"""
    logger.info(f"开始验证订阅链接: {url}")
    for attempt in range(CONFIG["subscription"]["retry_times"]):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            # 验证内容是否有效
            if is_valid_subscription_content(response.text):
                logger.info(f"订阅链接可访问且内容有效: {url}")
                return True
            else:
                logger.warning(f"订阅链接可访问但内容无效: {url}")
                return False

        except Exception as e:
            logger.error(
                f"验证订阅链接失败 (尝试 {attempt + 1}/{CONFIG['subscription']['retry_times']}): {url} - {e}"
            )
            if attempt < CONFIG["subscription"]["retry_times"] - 1:
                time.sleep(CONFIG["subscription"]["retry_delay"])
            continue
    return False


def process_subscriptions(gist_service):
    """处理所有订阅，返回可用的订阅 URL 列表"""
    logger.info("开始处理所有订阅")
    data = gist_service.load_subscriptions()
    valid_urls = []

    # 创建线程池
    with ThreadPoolExecutor(
        max_workers=CONFIG["subscription"]["max_workers"]
    ) as executor:
        # 创建任务列表
        future_to_sub = {
            executor.submit(
                gist_service.update_subscription_status, sub, fetch_nodes
            ): sub
            for sub in data.get("subscriptions", [])
            if sub.get("status") != "expired"
            and not (
                sub.get("first_failure")
                and (datetime.now() - datetime.fromisoformat(sub["first_failure"])).days
                > CONFIG["subscription"]["max_failure_days"]
            )
        }

        # 处理完成的任务
        for future in as_completed(future_to_sub):
            sub = future_to_sub[future]
            try:
                if future.result():
                    valid_urls.append(sub['url'])
            except Exception as e:
                logger.error(f"处理订阅时出错 {sub['url']}: {e}")

    # 保存更新后的订阅信息
    gist_service.save_subscriptions(data)
    logger.info(f"所有订阅处理完成，找到 {len(valid_urls)} 个有效订阅")
    return valid_urls

def main():
    parser = argparse.ArgumentParser(description="Merge and convert subscriptions")
    parser.add_argument("--gist-id", required=True, help="Gist ID")
    parser.add_argument("--token", required=True, help="GitHub token")
    args = parser.parse_args()

    logger.info("启动订阅收集和更新程序")

    # 初始化服务
    gist_service = GistService(args.token)

    gist_service.downloadGistFile(args.gist_id, "subscriptions.json")

    telegram_service = TelegramService(CONFIG["telegram"]["channels"])
    # 处理 Telegram 历史消息
    telegram_service.process_telegram_messages(gist_service)

    urls = process_subscriptions(gist_service)

    if urls:
        gist_service.uploadGistFile(args.gist_id, "subscribes-scan.txt", '\n'.join(urls))

    with open("subscriptions.json", "r", encoding="utf-8") as f:
        gist_service.uploadGistFile(args.gist_id, "subscriptions.json", f.read().strip())        

if __name__ == "__main__":
    main()
