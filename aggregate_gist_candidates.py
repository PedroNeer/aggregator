#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml


API_ROOT = "https://api.github.com"
DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
IGNORED_IDENTITY_FIELDS = {
    "name",
    "tfo",
    "udp",
    "mptcp",
    "smux",
    "dialer-proxy",
    "interface-name",
    "routing-mark",
}


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return dt.datetime.fromisoformat(value).astimezone(dt.timezone.utc)


def headers(token=None):
    result = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sub-candidate-aggregator",
    }
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def request_json(url, token=None):
    req = urllib.request.Request(url, headers=headers(token))
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_text(url, token=None):
    req = urllib.request.Request(url, headers=headers(token))
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_text_source(source, token=None):
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        return request_text(source, token)
    if parsed.scheme == "file":
        path = urllib.request.url2pathname(parsed.path)
    else:
        path = source
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def choose_file(files, filename):
    if filename:
        item = files.get(filename)
        if not item:
            available = ", ".join(sorted(files.keys()))
            raise RuntimeError(f"source filename {filename!r} not found; available: {available}")
        return item

    yaml_files = [
        item
        for name, item in files.items()
        if name.lower().endswith((".yaml", ".yml"))
    ]
    if yaml_files:
        return yaml_files[0]
    if files:
        return next(iter(files.values()))
    raise RuntimeError("gist revision has no files")


def gist_revision_text(gist_id, revision_sha, filename, token=None):
    revision = request_json(f"{API_ROOT}/gists/{gist_id}/{revision_sha}", token)
    item = choose_file(revision.get("files") or {}, filename)
    if item.get("content") and not item.get("truncated"):
        return item["content"]
    raw_url = item.get("raw_url")
    if not raw_url:
        raise RuntimeError(f"file {item.get('filename')!r} has no raw_url")
    return request_text(raw_url, token)


def recent_gist_revisions(gist_id, hours, max_revisions=0, token=None):
    cutoff = utc_now() - dt.timedelta(hours=hours)
    revisions = []
    page = 1
    while True:
        url = f"{API_ROOT}/gists/{gist_id}/commits?per_page=100&page={page}"
        batch = request_json(url, token)
        if not batch:
            break

        saw_old = False
        for item in batch:
            committed_at = parse_time(item["committed_at"])
            if committed_at >= cutoff:
                revisions.append(
                    {
                        "sha": item["version"],
                        "committed_at": committed_at,
                        "url": item.get("url"),
                    }
                )
                if max_revisions and len(revisions) >= max_revisions:
                    return revisions
            else:
                saw_old = True

        if saw_old or len(batch) < 100:
            break
        page += 1

    return revisions


def extract_proxies(text, source_label):
    data = yaml.safe_load(text)
    if isinstance(data, dict):
        proxies = data.get("proxies")
    elif isinstance(data, list):
        proxies = data
    else:
        proxies = None

    if not isinstance(proxies, list):
        raise RuntimeError(f"{source_label}: no proxies list found")

    result = []
    for item in proxies:
        if not isinstance(item, dict):
            continue
        if not item.get("type") or not item.get("server") or item.get("port") is None:
            continue
        result.append(dict(item))
    return result


def canonicalize(value):
    if isinstance(value, dict):
        return {
            key: canonicalize(value[key])
            for key in sorted(value)
            if value[key] is not None and key not in IGNORED_IDENTITY_FIELDS
        }
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    return value


def fingerprint(proxy):
    body = json.dumps(canonicalize(proxy), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def short_hash(value, size=8):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:size]


def dedupe(records):
    by_fingerprint = {}
    for proxy, seen_at, source in records:
        fp = fingerprint(proxy)
        existing = by_fingerprint.get(fp)
        if existing is None:
            by_fingerprint[fp] = {
                "proxy": proxy,
                "first_seen": seen_at,
                "last_seen": seen_at,
                "seen": 1,
                "sources": {source},
                "fingerprint": fp,
            }
            continue

        existing["seen"] += 1
        existing["sources"].add(source)
        if seen_at > existing["last_seen"]:
            existing["last_seen"] = seen_at
            existing["proxy"] = proxy
        if seen_at < existing["first_seen"]:
            existing["first_seen"] = seen_at

    rows = list(by_fingerprint.values())
    rows.sort(
        key=lambda row: (
            -row["seen"],
            -int(row["last_seen"].timestamp()),
            str(row["proxy"].get("name") or ""),
        )
    )
    return rows


def ensure_unique_names(rows):
    used = set()
    output = []
    for row in rows:
        proxy = dict(row["proxy"])
        base = str(proxy.get("name") or "").strip()
        if not base:
            base = f"{proxy.get('type')}-{proxy.get('server')}-{proxy.get('port')}"
        name = base
        if name in used:
            name = f"{base}-{row['fingerprint'][:6]}"
        while name in used:
            name = f"{base}-{short_hash(name + row['fingerprint'])}"
        proxy["name"] = name
        used.add(name)
        output.append(proxy)
    return output


def build_candidate_yaml(proxies):
    return yaml.safe_dump(
        {"proxies": proxies},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )


def build_metadata(args, stats):
    return {
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "retention_hours": args.retention_hours,
        "source_gist_id": args.source_gist_id,
        "source_filename": args.source_filename,
        "source_count": stats["source_count"],
        "revision_count": stats["revision_count"],
        "raw_proxy_count": stats["raw_proxy_count"],
        "unique_proxy_count": stats["unique_proxy_count"],
        "output_filename": args.output_filename,
    }


def patch_gist(gist_id, token, files, description=None):
    payload = {"files": {name: {"content": content} for name, content in files.items()}}
    if description:
        payload["description"] = description

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_ROOT}/gists/{gist_id}",
        data=body,
        method="PATCH",
        headers={
            **headers(token),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_records(args):
    records = []
    source_count = 0
    revision_count = 0

    if args.source_gist_id:
        revisions = recent_gist_revisions(
            args.source_gist_id,
            args.retention_hours,
            args.max_revisions,
            args.source_token,
        )
        if not revisions:
            raise RuntimeError(f"no gist revisions found in last {args.retention_hours} hours")

        for revision in revisions:
            label = f"gist:{args.source_gist_id}@{revision['sha'][:8]}"
            try:
                text = gist_revision_text(
                    args.source_gist_id,
                    revision["sha"],
                    args.source_filename,
                    args.source_token,
                )
                proxies = extract_proxies(text, label)
            except Exception as exc:
                print(f"warning: skipped {label}: {exc}", file=sys.stderr)
                continue
            source_count += 1
            revision_count += 1
            for proxy in proxies:
                records.append((proxy, revision["committed_at"], label))

    for source in args.source:
        label = source
        text = load_text_source(source, args.source_token)
        proxies = extract_proxies(text, label)
        source_count += 1
        for proxy in proxies:
            records.append((proxy, utc_now(), label))

    return records, source_count, revision_count


def parse_args():
    parser = argparse.ArgumentParser(
        description="Aggregate recent Clash/Mihomo proxies from Gist revisions or raw YAML sources."
    )
    parser.add_argument("--source-gist-id", default=os.getenv("SOURCE_GIST_ID", ""))
    parser.add_argument("--source-filename", default=os.getenv("SOURCE_FILENAME", ""))
    parser.add_argument("--target-gist-id", default=os.getenv("TARGET_GIST_ID", ""))
    parser.add_argument("--output-filename", default=os.getenv("OUTPUT_FILENAME", "candidate.yaml"))
    parser.add_argument("--metadata-filename", default=os.getenv("METADATA_FILENAME", "candidate-meta.json"))
    parser.add_argument("--retention-hours", type=int, default=int(os.getenv("RETENTION_HOURS", "24")))
    parser.add_argument("--max-revisions", type=int, default=int(os.getenv("MAX_REVISIONS", "30")))
    parser.add_argument("--min-proxies", type=int, default=int(os.getenv("MIN_PROXIES", "20")))
    parser.add_argument("--max-proxies", type=int, default=int(os.getenv("MAX_PROXIES", "0")))
    parser.add_argument("--source-token", default=os.getenv("SOURCE_GIST_TOKEN") or os.getenv("GIST_TOKEN", ""))
    parser.add_argument("--target-token", default=os.getenv("TARGET_GIST_TOKEN") or os.getenv("GIST_TOKEN", ""))
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "") == "1")
    parser.add_argument("--source", action="append", default=[])

    env_sources = os.getenv("SOURCE_URLS") or os.getenv("SOURCE_URL") or ""
    for source in env_sources.replace(",", "\n").splitlines():
        source = source.strip()
        if source:
            parser.set_defaults(source=[*parser.get_default("source"), source])

    return parser.parse_args()


def main():
    args = parse_args()
    if args.retention_hours <= 0:
        raise RuntimeError("retention hours must be positive")
    if not args.source_gist_id and not args.source:
        raise RuntimeError("set SOURCE_GIST_ID or SOURCE_URL/SOURCE_URLS")

    records, source_count, revision_count = collect_records(args)
    if not records:
        raise RuntimeError("no proxies extracted from sources")

    rows = dedupe(records)
    if args.max_proxies and len(rows) > args.max_proxies:
        rows = rows[: args.max_proxies]

    proxies = ensure_unique_names(rows)
    if len(proxies) < args.min_proxies:
        raise RuntimeError(f"only {len(proxies)} unique proxies; below MIN_PROXIES={args.min_proxies}")

    stats = {
        "source_count": source_count,
        "revision_count": revision_count,
        "raw_proxy_count": len(records),
        "unique_proxy_count": len(proxies),
    }
    metadata = build_metadata(args, stats)
    candidate = build_candidate_yaml(proxies)
    metadata_text = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as f:
            f.write("## Candidate subscription\n\n")
            for key, value in metadata.items():
                f.write(f"- `{key}`: `{value}`\n")

    if args.dry_run:
        with open(args.output_filename, "w", encoding="utf-8") as f:
            f.write(candidate)
        with open(args.metadata_filename, "w", encoding="utf-8") as f:
            f.write(metadata_text)
        return

    if not args.target_gist_id:
        raise RuntimeError("set TARGET_GIST_ID, or use --dry-run")
    if not args.target_token:
        raise RuntimeError("set TARGET_GIST_TOKEN or GIST_TOKEN")

    patch_gist(
        args.target_gist_id,
        args.target_token,
        {
            args.output_filename: candidate,
            args.metadata_filename: metadata_text,
        },
        description=f"Candidate subscription generated at {metadata['generated_at']}",
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
