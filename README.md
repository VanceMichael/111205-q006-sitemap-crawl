# Sitemap Crawl

A standard-library Python starter for sitemap crawl workflows. The current command surface is a callable service module; input samples in tests are synthetic.

## Run

Run the test suite with `python3 -m unittest discover -s . -p 'test_*.py' -v`. Import the service functions from `service.py` for local integration. No network access or third-party packages are required.

## Current behavior

The starter validates and normalizes one business record. Batch orchestration, persistence, recovery, and operational reporting are not yet included.
