#!/usr/bin/env bash
# ── DeepResearch 云服务器一键部署脚本 ─────────────
# 用法: sudo bash deploy/setup.sh
# 要求: 域名 DNS 已指向本服务器 IP，端口 80/443 可访问
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }

# ── 1. 检查 root ────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    err "请以 root 身份运行: sudo bash deploy/setup.sh"
    exit 1
fi

# ── 2. 安装 Docker（如未安装）─────────────────
if ! command -v docker &>/dev/null; then
    info "安装 Docker..."
    curl -fsSL https://get.docker.com | bash
    info "Docker 安装完成"
fi

if ! command -v docker compose &>/dev/null; then
    info "安装 Docker Compose plugin..."
    apt-get update && apt-get install -y docker-compose-plugin
fi

# ── 3. 进入项目目录 ────────────────────────────
cd "$(dirname "$0")/.."

# ── 4. 检查 .env ───────────────────────────────
if [ ! -f .env ]; then
    warn "未找到 .env，从 .env.example 复制"
    cp .env.example .env
    err "请编辑 .env 填入 DEEPSEEK_API_KEY，然后重新运行"
    exit 1
fi

if grep -q "sk-your-deepseek-api-key" .env 2>/dev/null; then
    err ".env 中 DEEPSEEK_API_KEY 仍是占位符，请填写真实密钥后重试"
    exit 1
fi

# ── 5. 输入域名 ─────────────────────────────────
read -rp "请输入域名（如 research.example.com）: " DOMAIN
DOMAIN="${DOMAIN%% }"
DOMAIN="${DOMAIN## }"
if [ -z "$DOMAIN" ]; then
    err "域名不能为空"
    exit 1
fi

info "域名: $DOMAIN"

# 替换 nginx 配置中的占位符
sed -i "s/example.com/$DOMAIN/g" deploy/nginx.conf
info "Nginx 配置已更新"

# ── 6. 申请 SSL 证书（Certbot 独立模式）───────
mkdir -p certbot/conf certbot/www

if [ -d "certbot/conf/live/$DOMAIN" ]; then
    info "SSL 证书已存在，跳过"
else
    info "申请 SSL 证书..."
    # 使用 standalone 模式（不需要先跑 nginx）
    docker run --rm \
        -p 80:80 \
        -v "$(pwd)/certbot/conf:/etc/letsencrypt" \
        -v "$(pwd)/certbot/www:/var/www/certbot" \
        certbot/certbot certonly --standalone \
        --email "admin@$DOMAIN" \
        --agree-tos \
        --no-eff-email \
        -d "$DOMAIN" || {
        err "证书申请失败。请确认:"
        err "  1. 域名 DNS 已指向本服务器 IP"
        err "  2. 端口 80 未被占用"
        exit 1
    }
    info "SSL 证书申请成功！"
fi

# ── 7. 构建并启动 ──────────────────────────────
info "构建并启动所有服务..."
docker compose up -d --build

# ── 8. 健康检查 ────────────────────────────────
info "等待服务就绪（约 15 秒）..."
sleep 10

for i in $(seq 1 6); do
    if curl -sf "https://$DOMAIN/health" > /dev/null 2>&1; then
        echo ""
        info "╔══════════════════════════════════════╗"
        info "║  ✅  DeepResearch 部署完成！         ║"
        info "║                                      ║"
        info "║  访问地址: https://$DOMAIN           "
        info "║  健康检查: https://$DOMAIN/health     "
        info "║                                      ║"
        info "║  日志查看: docker compose logs -f    ║"
        info "║  停止服务: docker compose down       ║"
        info "╚══════════════════════════════════════╝"
        exit 0
    fi
    sleep 5
done

warn "服务似乎未及时响应，请手动检查:"
warn "  docker compose logs"
warn "  curl https://$DOMAIN/health"
