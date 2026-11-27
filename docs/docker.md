# Docker Compose operations

The Compose stack defines exactly `rebalancer` and `ibkr-gateway`, with named volumes `rebalancer_state` and `ib_settings`.

```bash
docker compose up -d --build
docker compose logs -f rebalancer
docker compose logs -f ibkr-gateway
```

The dashboard is published only at `127.0.0.1:18778`. Gateway live, paper, and VNC mappings are also loopback-only by default. Use an SSH tunnel, private VPN, or separately managed HTTPS proxy for remote access.

Account configuration and management use the dashboard. Do not add bind mounts for `accounts.yaml`, `config.yaml`, or `data/`.

## Backups

Stop `rebalancer` or use SQLite's online backup operation from a trusted maintenance environment; do not copy a live database file blindly. Back up `rebalancer_state` and `ib_settings` separately. Take both backups before application or Gateway upgrades and verify restores in paper mode.

## Multiple installations

Set a unique `COMPOSE_PROJECT_NAME` and unique loopback ports per installation. Each Compose project then receives separate named volumes and a separate network. The dashboard host port is fixed by the supplied file; use a small reviewed Compose override for additional instances. Do not change the container listener from `18778`.
