#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import re
from datetime import datetime
import shutil
from fnmatch import fnmatch

from flask import Flask, Response, request, jsonify

# python3 -m cProfile -s cumtime ./log.py > profile.txt
RESULT_DIRECTORY_PATH = "./out"


class Detail:
    def __init__(self, filename, level, thread, dt, content, raw):
        self.filename = filename
        self.level = level
        self.thread = thread
        self.dt = dt
        self.content = content
        self.raw = raw

    def __str__(self):
        return f"Detail(filename={self.filename}, dt={self.dt}, level={self.level}, thread={self.thread}, content={self.content[:50]})"

    def __repr__(self):
        return f"Detail('{self.filename}', '{self.level}', '{self.thread}', '{self.dt}', '{self.content}')"

    def to_dict(self):
        return {
            "level": self.level,
            "thread": self.thread,
            "timestamp": self.dt,
            "raw": self.raw,
        }


pattern = re.compile(
    r"^\s*\[([A-Za-z0-9]+)\s*([A-Za-z0-9\s_\-!@#$%^&*()_+|<?.:=\[\],]+?),(\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d+)\]:(.*)$"
)


def read_file(files, include_dt=False):
    detail = None
    year = datetime.now().year
    for file in files[:]:
        print(f"Reading: {file}")
        should_concat_line = False
        with file.open(mode="r", encoding="UTF-8") as f:
            for line in f:
                m = pattern.match(line)
                if m:
                    if detail:
                        yield detail
                        detail = None

                    lv, thread, dt, content = m.group(1, 2, 3, 4)
                    if include_dt:
                        dt = datetime.fromisoformat(f"{year}-{dt}")
                    detail = Detail(file, lv, thread, dt, content, line.strip())
                    should_concat_line = True
                elif detail:
                    if should_concat_line:
                        detail.content += line
                        detail.raw += line
    if detail:
        yield detail


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Filter log details based on various criteria."
    )
    parser.add_argument("input_file", nargs="+", help="Filter by file")
    parser.add_argument("-l", "--level", type=int, help="Filter by log level")
    parser.add_argument(
        "-t",
        "--thread",
        nargs="+",
        help="Filter by one or more threads. Support regex.",
    )
    parser.add_argument(
        "-s", "--start-time", help="Start time for filtering (MM-DD HH:MM:SS format)"
    )
    parser.add_argument(
        "-e", "--end-time", help="End time for filtering (MM-DD HH:MM:SS format)"
    )
    # parser.add_argument("-c", "--content", help="Filter by content (case-insensitive substring match)")
    parser.add_argument(
        "-g",
        "--glob",
        help="Filter by file name. Support regex.",
        default=r"^WV-\w+-\d+-\d+\.log$",
    )
    parser.add_argument(
        "--web", action="store_true", help="Launch a web UI to view logs"
    )
    args = parser.parse_args()

    if args.start_time:
        args.start_time = datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S.%f")
    if args.end_time:
        args.end_time = datetime.strptime(args.end_time, "%Y-%m-%d %H:%M:%S.%f")
    if args.thread:
        args.thread = [re.compile(pat) for pat in args.thread]
    if args.glob:
        args.glob = re.compile(args.glob)

    return args


def filter_file(file, args):
    if args.glob:
        return args.glob.match(file.name)
    return True


def filter_detail(detail, args):
    if args.level and detail.lv != args.level:
        return False
    if args.thread:
        if not any(pat.match(detail.thread) for pat in args.thread):
            return False
    if args.start_time or args.end_time:
        if args.start_time and detail.dt < args.start_time:
            return False
        if args.end_time and detail.dt > args.end_time:
            return False
    if args.content:
        if not args.content.search(detail.content):
            return False
    return True


def path_to_files(input_file):
    files = []
    for input_path in args.input_file:
        path = Path(input_path)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for file in path.rglob("*"):  # non-recursive
                if file.is_file():
                    files.append(file)
    files = [f for f in files if filter_file(f, args)]
    files = sorted(files)
    return files


def cli_main(args):
    files = path_to_files(args.input_file)
    out_path = Path(RESULT_DIRECTORY_PATH)
    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir()

    out_idx = 0
    out_size = 0
    out_size_limit = 1024 * 1024 * 10
    f = None
    for detail in read_file(files, args.start_time or args.end_time):
        if not f or out_size > out_size_limit:
            f = open(
                os.path.join(RESULT_DIRECTORY_PATH, f"result_{out_idx}.log"),
                mode="w",
                encoding="utf-8",
            )
            out_size = 0
            out_idx += 1
        if filter_detail(detail, args):
            f.write(detail.raw)
            out_size += len(detail.raw.encode("utf-8"))


app = Flask(__name__)
search_index = None


def init_index():
    args = parse_arguments()
    index = {}
    files = path_to_files(args.input_file)
    for detail in read_file(files, include_dt=True):
        filename = str(detail.filename)
        if filename not in index:
            index[filename] = {
                "min_datetime": None,
                "max_datetime": None,
                "thread": set(),
            }

        if (
            index[filename]["min_datetime"] is None
            or detail.dt < index[filename]["min_datetime"]
        ):
            index[filename]["min_datetime"] = detail.dt

        if (
            index[filename]["max_datetime"] is None
            or detail.dt > index[filename]["max_datetime"]
        ):
            index[filename]["max_datetime"] = detail.dt

        index[filename]["thread"].add(detail.thread)
    global search_index
    search_index = index


@app.route("/logs")
def logs():
    if not search_index:
        return Response("Index not found", content_type="text/plain")

    args = parse_arguments()
    level = request.args.get("level")
    thread = request.args.get("thread")
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")
    content = request.args.get("content")

    def parse_time(time_str, default_year=None):
        if not time_str:
            return None
        try:
            dt = datetime.strptime(time_str, "%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                dt = datetime.strptime(time_str, "%m-%d %H:%M:%S")
            except ValueError:
                # Handle other formats or raise an error
                raise ValueError(f"Invalid time format: {time_str}")
        if default_year is not None:
            dt = dt.replace(year=default_year)
        return dt

    # Create a Namespace object with the query parameters
    reqargs = argparse.Namespace(
        level=level if level else None,
        thread=[re.compile(thread)] if thread else None,
        start_time=(
            parse_time(start_time, default_year=datetime.now().year)
            if start_time
            else None
        ),
        end_time=(
            parse_time(end_time, default_year=datetime.now().year) if end_time else None
        ),
        content=re.compile(content) if content else None,
    )

    def generate_logs():
        def filter_file(file):
            pathname = str(file)
            index = search_index.get(pathname)
            if not index:
                return True
            if reqargs.thread:
                anyMatch = False
                for thread in index["thread"]:
                    for pattern in reqargs.thread:
                        if pattern.match(thread):
                            anyMatch = True
                if not anyMatch:
                    return False
            if reqargs.start_time and reqargs.start_time > index["max_datetime"]:
                return False
            if reqargs.end_time and reqargs.end_time < index["min_datetime"]:
                return False
            return True

        files = path_to_files(args.input_file)
        files = [file for file in files if filter_file(file)]

        result_count = 0
        for detail in read_file(files, reqargs.start_time or reqargs.end_time):
            if filter_detail(detail, reqargs):
                result_count += 1
                if (
                    not (reqargs.start_time and reqargs.end_time)
                    and result_count > 1000
                ):
                    break
                yield detail.to_dict()

    return jsonify(list(generate_logs()))


@app.route("/")
def index():
    template = r"""<!DOCTYPE html>
<html>
<head>
    <title>Log Viewer</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body, input, button {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
        }

        pre, pre * {
            font-family: 'Liberation Mono', 'Menlo', 'Monaco', 'Consolas', 'Liberation Mono', 'Courier New', monospace !important;
            white-space: pre;
        }

        html, body {
            height: 100%;
            overflow: hidden;
        }

        body {
            background-color: #f8f9fa;
            color: #212529;
            padding: 0.5rem;
            display: flex;
            flex-direction: column;
        }

        h1 {
            color: #2c3e50;
            margin-bottom: 0.5rem;
            font-size: 1.25rem;
            font-weight: 500;
        }

        #filter-form {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 0.5rem;
            margin-bottom: 0.5rem;
            background: white;
            padding: 0.75rem;
            border-radius: 4px;
            box-shadow: 0 1px 2px rgba(0,0,0,0.1);
        }

        input {
            width: 100%;
            padding: 0.4rem;
            border: 1px solid #dee2e6;
            border-radius: 3px;
            font-size: 0.8rem;
        }

        input:focus {
            outline: none;
            border-color: #4299e1;
            box-shadow: 0 0 0 2px rgba(66, 153, 225, 0.2);
        }

        button {
            background-color: #4299e1;
            color: white;
            padding: 0.4rem 1rem;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            font-size: 0.8rem;
            font-weight: 500;
        }

        button:hover {
            background-color: #3182ce;
        }

        pre {
            background-color: white;
            padding: 0.75rem;
            border-radius: 4px;
            border: 1px solid #dee2e6;
            flex-grow: 1;
            overflow-y: auto;
            font-size: 0.8rem;
            line-height: 1.4;  /* slightly increased for better readability */
            white-space: pre;  /* changed from pre-wrap to preserve exact spacing */
            box-shadow: 0 1px 2px rgba(0,0,0,0.1);
        }

        pre::-webkit-scrollbar {
            width: 6px;
        }

        pre::-webkit-scrollbar-track {
            background: #f1f1f1;
        }

        pre::-webkit-scrollbar-thumb {
            background: #cbd5e0;
            border-radius: 3px;
        }

        pre::-webkit-scrollbar-thumb:hover {
            background: #a0aec0;
        }

        .log-line {
            cursor: pointer;
            display: inline;
        }

        .log-line:hover {
            background-color: rgba(66, 153, 225, 0.1);
        }

        .loading {
            color: #718096;
            font-style: italic;
        }

        @media (max-width: 768px) {
            #filter-form {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <h1>Log Viewer</h1>
    <form id="filter-form">
        <input type="text" name="level" placeholder="Log Level">
        <input type="text" name="thread" placeholder="Thread ID">
        <input type="text" name="start_time" placeholder="Start Time">
        <input type="text" name="end_time" placeholder="End Time">
        <input type="text" name="content" placeholder="Search">
        <button type="submit">Filter</button>
    </form>
    <pre id="logs">Initializing log viewer...</pre>

    <script>
        function fetchLogs(url, params) {
            const logsElement = document.getElementById('logs');
            logsElement.classList.add('loading');
            logsElement.textContent = 'Fetching logs...';

            const queryString = new URLSearchParams(params).toString();
            fetch(`${url}?${queryString}`)
                .then(response => {
                    if (!response.ok) throw new Error('Network response was not ok');
                    return response.text();
                })
                .then(data => {
                    logsElement.classList.remove('loading');
                    if (!data) {
                        logsElement.textContent = 'No logs found';
                        return;
                    }

                    const resp = JSON.parse(data);
                    const formattedLogs = resp
                        .map(formatLogLine)
                        .join('\n');

                    logsElement.innerHTML = formattedLogs;
                    attachLogLineHandlers();
                })
                .catch(error => {
                    logsElement.classList.remove('loading');
                    logsElement.textContent = `Error: ${error.message}`;
                });
        }

        function updateFormInputs() {
            const currentUrl = window.location.href;
            const url = new URL(currentUrl);
            const params = new URLSearchParams(url.search);
            const paramsList = ['level', 'thread', 'start_time', 'end_time', 'content'];

            for (let paramName of paramsList) {
                if (params.get(paramName)) {
                    document.querySelector(`#filter-form>[name=${paramName}]`).value = params.get(paramName);
                } else {
                    document.querySelector(`#filter-form>[name=${paramName}]`).value = '';
                }
            }
        }
        
        function formatLogLine(line) {
            const timestamp = line['timestamp'];
            const thread = line['thread'];
            const content = line['raw'];
            return `<div class="log-line" data-timestamp="${timestamp}" data-thread="${thread}">${content}</div>`;
        }

        function attachLogLineHandlers() {
            document.querySelectorAll('.log-line').forEach(line => {
                line.addEventListener('click', () => {
                    const ts = line.dataset.timestamp;
                    if (!ts) return;

                    const params = new URLSearchParams({
                        start_time: ts,
                        thread: `${line.dataset.thread}$`,
                    });

                    window.open(`${window.location.pathname}?${params.toString()}`, '_blank');
                });
            });
        }

        document.getElementById('filter-form').addEventListener('submit', function(event) {
            event.preventDefault();
            const formData = new FormData(this);
            const params = Object.fromEntries(
                formData.entries().filter(([key, value]) => value !== '')
            );

            const queryString = new URLSearchParams(params).toString();
            const newUrl = `${window.location.pathname}?${queryString}`;
            window.history.pushState(null, '', newUrl);

            fetchLogs('/logs', params);
        });

        // Handle back/forward navigation
        window.addEventListener('popstate', () => {
            updateFormInputs(); // Update form inputs
            const currentUrl = window.location.href;
            const url = new URL(currentUrl);
            const params = new URLSearchParams(url.search);
            const formParams = Object.fromEntries(params.entries());
            fetchLogs('/logs', formParams); // Fetch logs
        });

        // Initial load: Update form inputs and fetch logs
        window.addEventListener('load', () => {
            updateFormInputs(); // Update form inputs
            const currentUrl = window.location.href;
            const url = new URL(currentUrl);
            const params = new URLSearchParams(url.search);
            const formParams = Object.fromEntries(params.entries());
            fetchLogs('/logs', formParams); // Fetch logs
        });
    </script>
</body>
</html>
"""
    return template


if __name__ == "__main__":
    args = parse_arguments()
    if args.web:
        import webbrowser

        # webbrowser.open("http://localhost:5000")
        init_index()
        app.run(use_reloader=False, port=28515)
    else:
        cli_main(args)
