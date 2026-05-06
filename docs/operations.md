# YC Radar Operations

This repo runs as an internal script pipeline on the EC2 host `yc-radar-ec2`.

## Hosts

```text
EC2 public IP: 54.91.53.20
EC2 Tailscale IP: 100.98.36.32
RDS endpoint: yc-radar-postgres.cmhoa6y2gmoi.us-east-1.rds.amazonaws.com
RDS database: yc_radar
RDS user: ycradar
```

Prefer Tailscale for SSH:

```bash
ssh -i ~/.ssh/yc_radar_ec2 ubuntu@100.98.36.32
```

Public SSH is only an emergency fallback and is restricted in the EC2 security group.

## Long-Running Pipeline

The EC2 host uses tmux sessions:

```bash
tmux ls
tmux attach -t yc-pipeline
tmux attach -t yc-worker
```

Detach from tmux with `Ctrl-b`, then `d`.

Useful logs:

```bash
tail -f /home/ubuntu/yc-radar/logs/pipeline.log
tail -f /home/ubuntu/yc-radar/logs/worker.log
```

The pipeline stages are:

```text
load snapshots -> discover career URLs -> enqueue classification tasks -> Celery classification
```

Discovery is a normal Python script. Classification is queued through Celery, backed by Redis, and
visible in Flower.

## Flower

Flower runs on the EC2 host:

```text
http://100.98.36.32:5555
```

If you prefer a localhost URL, open a tunnel:

```bash
ssh -i ~/.ssh/yc_radar_ec2 -L 5555:localhost:5555 ubuntu@100.98.36.32
```

Then open:

```text
http://localhost:5555
```

Flower can show zero processed tasks while discovery is still running. That is expected because
classification tasks are enqueued only after discovery finishes.

## RDS And TablePlus

RDS is private inside the AWS VPC. Connect through the EC2 host with an SSH tunnel:

```bash
ssh -i ~/.ssh/yc_radar_ec2 \
  -L 5432:yc-radar-postgres.cmhoa6y2gmoi.us-east-1.rds.amazonaws.com:5432 \
  ubuntu@100.98.36.32
```

TablePlus settings:

```text
Host: localhost
Port: 5432
User: ycradar
Database: yc_radar
SSL: required
```

The RDS password is stored only on the EC2 host:

```bash
cat /home/ubuntu/yc-radar/data/local/secrets/rds_yc_radar_password
```

Copy it to the Mac clipboard:

```bash
ssh -i ~/.ssh/yc_radar_ec2 ubuntu@100.98.36.32 \
  'cat /home/ubuntu/yc-radar/data/local/secrets/rds_yc_radar_password' | pbcopy
```

## Deploys

GitHub Actions runs tests and Ruff. Pushes to `main` deploy to EC2 over SSH and rsync.

The deploy script:

```bash
scripts/deploy_ec2.sh
```

It restarts the Celery worker by default. It does not restart the long-running pipeline unless
`RESTART_PIPELINE=1` is set, which prevents accidental interruption of overnight runs.

Required GitHub secrets:

```text
EC2_HOST
EC2_USER
EC2_SSH_KEY
```

Optional GitHub variable:

```text
EC2_APP_DIR=/home/ubuntu/yc-radar
```
