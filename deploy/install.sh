#!/usr/bin/env bash
# 手动安装，任何带 systemd 的 Linux 都能跑。
# 只有一台机器、不想为这么个小服务引入 Ansible 的话用这个。
#
# 用法：
#   sudo bash deploy/install.sh                     # 在本仓库根目录执行
#   sudo APP_EXPORTER_PORT=9878 bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/app-exporter
ENV_FILE=/etc/app-exporter.env
SERVICE=app-exporter.service
PORT="${APP_EXPORTER_PORT:-9877}"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root：sudo bash deploy/install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> 源码目录：$SRC_DIR"

# 1) 专用低权限用户。已存在就跳过（幂等，重复执行不会报错）
if ! id exporter &>/dev/null; then
  echo "==> 创建 exporter 用户"
  useradd --system --shell /sbin/nologin --no-create-home exporter
fi

# 2) 依赖
echo "==> 安装 Python 依赖"
if command -v pip3 >/dev/null; then
  pip3 install --quiet prometheus_client requests
else
  echo "找不到 pip3，请先装 python3-pip（Rocky/RHEL: dnf install -y python3-pip）" >&2
  exit 1
fi

# 3) 代码
echo "==> 部署到 $APP_DIR"
install -d -o exporter -g exporter -m 0755 "$APP_DIR"
install -o exporter -g exporter -m 0644 "$SRC_DIR/app_exporter.py" "$APP_DIR/app_exporter.py"

# 4) 参数文件。已存在就不覆盖——避免把线上改过的配置冲掉
if [[ -f "$ENV_FILE" ]]; then
  echo "==> $ENV_FILE 已存在，保留不动"
else
  echo "==> 生成 $ENV_FILE"
  sed "s/^APP_EXPORTER_PORT=.*/APP_EXPORTER_PORT=${PORT}/" \
    "$SRC_DIR/deploy/app-exporter.env.example" > "$ENV_FILE"
  chmod 0644 "$ENV_FILE"
fi

# 5) 服务
echo "==> 安装 systemd 服务"
install -m 0644 "$SRC_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

# 6) 自检：端口通 + 指标有内容，不然别报成功
echo "==> 自检"
for i in $(seq 1 10); do
  if curl -sf "http://127.0.0.1:${PORT}/metrics" -o /tmp/app_exporter_metrics.txt 2>/dev/null; then
    break
  fi
  sleep 1
done

if grep -q '^app_queue_depth' /tmp/app_exporter_metrics.txt 2>/dev/null; then
  echo "==> 安装成功。检查 Prometheus 抓取目标里是否已加入 本机IP:${PORT}"
  echo "    常用排查：journalctl -u app-exporter -n 50 --no-pager"
else
  echo "!! 端口起来了但没抓到 app_ 指标，看日志：" >&2
  journalctl -u app-exporter -n 30 --no-pager >&2 || true
  exit 1
fi
