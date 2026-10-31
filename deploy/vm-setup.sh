#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VM, run from the repository root:  bash deploy/vm-setup.sh
# Needs ports 80 and 443 open. Uses <public-ip-with-dashes>.sslip.io unless HOST is set.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi

IP="$(curl -fsS https://api.ipify.org)"
HOST="${HOST:-$(echo "$IP" | tr . -).sslip.io}"

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')|" .env
fi
sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$HOST|; s|^SITE_ADDRESS=.*|SITE_ADDRESS=$HOST|" .env

sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

echo "Waiting for the database to load the seed data..."
until sudo docker compose exec -T app python -c "import app; app.get_db().close()" >/dev/null 2>&1; do sleep 5; done
sleep 10

ASSESSOR_PASSWORD="$(openssl rand -base64 15 | tr -d '/+=')"
sudo docker compose exec -T -e OFFICER_PASSWORD="$ASSESSOR_PASSWORD" app \
  python manage_officers.py assessor "Assessor (District Social Welfare Officer)" "*"

echo
echo "Portal:            https://$HOST"
echo "Citizen MCP:       https://$HOST/mcp/citizen"
echo "Officer MCP:       https://$HOST/mcp/officer"
echo "Eval dashboard:    https://$HOST/eval-dashboard"
echo "Assessor login:    assessor / $ASSESSOR_PASSWORD   (write this down; it is not stored in plain text)"
