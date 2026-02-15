# deploy-watcher

Post-deployment health monitor that watches your services after deploy and provides auto-rollback capabilities on failure detection.

## Features

- **HTTP Health Checks**: Monitor endpoints with configurable intervals
- **Multi-service**: Watch multiple services simultaneously
- **Threshold-based alerts**: Consecutive failure count triggers alerts
- **Webhook notifications**: Slack, Discord, or custom webhooks
- **Auto-rollback**: Execute rollback commands when health check fails
- **Timeout handling**: Configurable request timeouts
- **Structured logging**: JSON-formatted logs for log aggregation
- **YAML config**: Simple, readable configuration

## Quick Start

```bash
pip install -r requirements.txt
python watcher.py --config config.yaml
```

## Configuration

```yaml
# config.yaml
global:
  check_interval: 10      # seconds between checks
  timeout: 5              # request timeout
  failure_threshold: 3    # consecutive failures before alert

services:
  - name: api-gateway
    url: https://api.example.com/health
    method: GET
    expected_status: 200
    expected_body: '"status":"ok"'
    
  - name: auth-service
    url: https://auth.example.com/ping
    method: GET
    expected_status: 200

notifications:
  slack:
    webhook_url: https://hooks.slack.com/services/xxx
    channel: "#deployments"
  
  webhook:
    url: https://your-webhook.example.com/alert
    method: POST

rollback:
  enabled: true
  command: "./scripts/rollback.sh"
  cooldown: 300   # seconds between rollback attempts
```

## Architecture

```
                   +------------------+
                   |  deploy-watcher  |
                   +--------+---------+
                            |
              +-------------+-------------+
              |             |             |
        +-----v----+  +----v-----+  +----v-----+
        |  Service  |  |  Service |  |  Service |
        |  Checker  |  |  Checker |  |  Checker |
        +-----+----+  +----+-----+  +----+-----+
              |             |             |
              +------+------+------+------+
                     |             |
               +-----v----+  +----v--------+
               | Notifier |  | Rollback    |
               | (Slack/  |  | Engine      |
               |  Webhook)|  |             |
               +----------+  +-------------+
```

## Usage

```bash
# Basic usage
python watcher.py --config config.yaml

# Verbose mode
python watcher.py --config config.yaml --verbose

# Single check (CI/CD pipeline)
python watcher.py --config config.yaml --once

# Dry run (test config)
python watcher.py --config config.yaml --dry-run
```

## Requirements

- Python 3.9+
- See requirements.txt

## License

MIT