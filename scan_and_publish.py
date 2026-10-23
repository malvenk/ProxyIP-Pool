#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动维护优选 IP 上游的核心脚本。

流程：
  1. 从 Cloudflare 官方拉取 IPv4 段
  2. 下载 CloudflareSpeedTest（固定版本 v2.2.5，避免 latest 被替换成有问题的版本）
  3. 跑测速，拿到按下载速度排序的结果
  4. 挑出达标的 IP，用免费 Geo API 批量查国家代码
  5. 输出成 "IP:PORT#国家代码" 格式，一行一笔，写入 OUTPUT_FILE

用法（本地或 GitHub Actions 里）：
  python3 scan_and_publish.py

可用环境变量调整行为，见下面 CONFIG 区块。
"""

import csv
import os
import platform
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

# ===================== 可调整设定 =====================
CF_SPEEDTEST_VERSION = "v2.2.5"  # 固定版本号，不用 latest，避免上游突然替换文件
PORT = int(os.environ.get("CF_PORT", "443"))  # 测速 & 最终输出使用的端口
MIN_DOWNLOAD_MBPS = float(os.environ.get("MIN_DOWNLOAD_MBPS", "2"))  # 下载速度门槛，低于这个不收录
MAX_LATENCY_MS = float(os.environ.get("MAX_LATENCY_MS", "200"))  # 平均延迟门槛
TOP_N = int(os.environ.get("TOP_N", "80"))  # 最多收录多少个 IP（避免清单过大、也省 Geo API 额度）
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "ips/all.txt")
RESULT_CSV = "result.csv"
IP_LIST_FILE = "ip.txt"
# =======================================================


def 下载文件(url: str, dest: str, timeout: int = 30) -> None:
    print(f"[下载] {url} -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "scan-and-publish-script"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def 取得官方IPv4段(dest: str = IP_LIST_FILE) -> None:
    下载文件("https://www.cloudflare.com/ips-v4/", dest)
    with open(dest) as f:
        count = sum(1 for line in f if line.strip())
    print(f"[信息] 官方 IPv4 段共 {count} 条（含子网，CloudflareST 会自动展开抽样）")


def 判断平台标识() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system != "linux":
        raise RuntimeError(f"本脚本目前只支持在 Linux（如 GitHub Actions ubuntu-latest）上运行，当前系统是 {system}")
    if machine in ("x86_64", "amd64"):
        return "linux_amd64"
    if machine in ("aarch64", "arm64"):
        return "linux_arm64"
    raise RuntimeError(f"未适配的架构：{machine}，请到 CloudflareSpeedTest 发布页自行找对应文件名")


def 下载并解压测速工具() -> str:
    平台标识 = 判断平台标识()
    文件名 = f"CloudflareST_{平台标识}.tar.gz"
    url = (
        f"https://github.com/XIU2/CloudflareSpeedTest/releases/download/"
        f"{CF_SPEEDTEST_VERSION}/{文件名}"
    )
    下载文件(url, 文件名)
    with tarfile.open(文件名, "r:gz") as tar:
        tar.extractall(".")
    binary = "./CloudflareST"
    os.chmod(binary, 0o755)
    return binary


def 跑测速(binary: str) -> None:
    print("[测速] 开始，可能需要几分钟…")
    cmd = [
        binary,
        "-f", IP_LIST_FILE,
        "-o", RESULT_CSV,
        "-tp", str(PORT),
        "-tl", str(int(MAX_LATENCY_MS)),   # 平均延迟上限（ms）
        "-sl", str(MIN_DOWNLOAD_MBPS),      # 下载速度下限（MB/s）
        "-dn", "20",                        # 下载测速数量：凑够 20 个达标就停
        "-dt", "6",                         # 单个 IP 下载测速最长秒数（控制总耗时）
        "-p", "0",                          # 终端不打印结果表格，只看 CSV
    ]
    subprocess.run(cmd, check=True)
    print("[测速] 完成")


def 读取测速结果() -> list[dict]:
    with open(RESULT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"[信息] 测速结果共 {len(rows)} 条达标 IP")
    return rows[:TOP_N]


def 批量查国家代码(ip_list: list[str]) -> dict:
    """用 ip-api.com 的免费批量接口查国家代码。免费额度：45 次/分钟，每次最多 100 个 IP。"""
    结果 = {}
    CHUNK = 100
    for i in range(0, len(ip_list), CHUNK):
        批次 = ip_list[i:i + CHUNK]
        print(f"[Geo 查询] 第 {i // CHUNK + 1} 批，共 {len(批次)} 个 IP")
        payload = [{"query": ip, "fields": "query,countryCode,status"} for ip in 批次]
        import json
        req = urllib.request.Request(
            "http://ip-api.com/batch",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            数据 = json.loads(resp.read())
        for item in 数据:
            if item.get("status") == "success":
                结果[item["query"]] = item.get("countryCode", "")
        if i + CHUNK < len(ip_list):
            time.sleep(2)  # 避免触发免费额度限制
    return 结果


def 找出CSV里的IP字段名(fieldnames: list[str]) -> str:
    for 候选 in ("IP 地址", "IP Address", "ip", "IP"):
        if 候选 in fieldnames:
            return 候选
    # 兜底：直接用第一个字段
    return fieldnames[0]


def 主流程() -> None:
    取得官方IPv4段()
    binary = 下载并解压测速工具()
    跑测速(binary)
    rows = 读取测速结果()

    if not rows:
        print("[警告] 本轮没有任何 IP 达标，保留上一次的结果文件，不覆盖")
        return

    ip字段 = 找出CSV里的IP字段名(rows[0].keys())
    ip_list = [row[ip字段].strip() for row in rows if row.get(ip字段, "").strip()]

    国家对照 = 批量查国家代码(ip_list)

    输出行 = []
    for ip in ip_list:
        国家 = 国家对照.get(ip)
        if not 国家:
            continue  # 查不到国家代码的跳过，避免产生 "IP#" 这种脏数据
        输出行.append(f"{ip}:{PORT}#{国家}")

    os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(输出行) + "\n")

    print(f"[完成] 写入 {len(输出行)} 条到 {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        主流程()
    except subprocess.CalledProcessError as e:
        print(f"[错误] 测速工具执行失败：{e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)
