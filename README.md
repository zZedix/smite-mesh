# Smite Mesh

## راهنمای نصب دستی

### پیش‌نیازها

برای نصب پنل و نود به موارد زیر نیاز دارید:

- **سیستم عامل**: Ubuntu 20.04 یا بالاتر، Debian 11 یا بالاتر
- **Docker**: نسخه 20.10 یا بالاتر
- **Docker Compose**: نسخه 2.0 یا بالاتر
- **Node.js**: نسخه 18 یا بالاتر (فقط برای پنل)
- **Git**: برای کلون کردن مخزن

### نصب پنل (Panel)

#### مرحله 1: نصب Docker و Docker Compose

```bash
# به‌روزرسانی سیستم
sudo apt update && sudo apt upgrade -y

# نصب Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# افزودن کاربر به گروه Docker
sudo usermod -aG docker $USER

# نصب Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# راه‌اندازی مجدد یا لاگ‌اوت برای اعمال تغییرات
newgrp docker
```

#### مرحله 2: نصب Node.js

```bash
# نصب Node.js 18
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs

# بررسی نسخه
node --version
npm --version
```

#### مرحله 3: کلون کردن مخزن و آماده‌سازی

```bash
# کلون کردن مخزن
git clone https://github.com/zZedix/smite-mesh.git
cd smite-mesh

# ایجاد دایرکتوری نصب
sudo mkdir -p /opt/smite
sudo cp -r * /opt/smite/
cd /opt/smite
```

#### مرحله 4: ساخت Frontend

```bash
cd frontend

# نصب وابستگی‌ها
npm install

# ساخت Frontend برای تولید
npm run build

cd ..
```

#### مرحله 5: تنظیم فایل محیطی (Environment)

```bash
# ایجاد فایل .env
cat > .env << EOF
SMITE_VERSION=main
PANEL_PORT=8000
EOF

# یا ویرایش دستی فایل .env
nano .env
```

#### مرحله 6: راه‌اندازی پنل با Docker Compose

```bash
# ساخت و راه‌اندازی سرویس‌ها
docker compose up -d

# بررسی وضعیت سرویس‌ها
docker compose ps

# مشاهده لاگ‌ها
docker compose logs -f
```

#### مرحله 7: نصب ابزار CLI

```bash
# کپی فایل CLI
sudo cp cli/smite.py /usr/local/bin/smite
sudo chmod +x /usr/local/bin/smite

# بررسی نصب
smite --help
```

#### مرحله 8: ایجاد کاربر مدیر

```bash
# ایجاد کاربر مدیر اولیه
smite admin create
```

پنل اکنون در آدرس `http://localhost:8000` در دسترس است.

---

### نصب نود (Node)

#### مرحله 1: نصب Docker و Docker Compose

اگر Docker و Docker Compose نصب نیستند، مراحل نصب را از بخش "نصب پنل" دنبال کنید.

#### مرحله 2: کلون کردن مخزن

```bash
# کلون کردن مخزن
git clone https://github.com/zZedix/smite-mesh.git
cd smite-mesh
```

#### مرحله 3: آماده‌سازی دایرکتوری نصب

```bash
# ایجاد دایرکتوری نصب
sudo mkdir -p /opt/smite-node
sudo cp -r node/* /opt/smite-node/
cd /opt/smite-node
```

#### مرحله 4: تنظیم فایل محیطی

```bash
# ایجاد فایل .env
cat > .env << EOF
SMITE_VERSION=main
PANEL_URL=http://YOUR_PANEL_IP:8000
EOF

# ویرایش آدرس پنل
nano .env
```

**مهم**: `YOUR_PANEL_IP` را با آدرس IP واقعی سرور پنل جایگزین کنید.

#### مرحله 5: راه‌اندازی نود

```bash
# ساخت و راه‌اندازی نود
docker compose up -d

# بررسی وضعیت
docker compose ps

# مشاهده لاگ‌ها
docker compose logs -f
```

#### مرحله 6: نصب ابزار CLI نود

```bash
# کپی فایل CLI
sudo cp ../cli/smite-node.py /usr/local/bin/smite-node
sudo chmod +x /usr/local/bin/smite-node

# بررسی نصب
smite-node --help
```

#### مرحله 7: بررسی اتصال به پنل

```bash
# بررسی وضعیت نود
smite-node status

# مشاهده لاگ‌ها
smite-node logs
```

نود اکنون به پنل متصل شده و آماده استفاده است.

---

### به‌روزرسانی

#### به‌روزرسانی پنل

```bash
cd /opt/smite
smite update
```

#### به‌روزرسانی نود

```bash
cd /opt/smite-node
smite-node update
```

---

### حذف نصب (Uninstall)

#### حذف پنل

```bash
cd /opt/smite
smite uninstall
sudo rm -rf /opt/smite
sudo rm /usr/local/bin/smite
```

#### حذف نود

```bash
cd /opt/smite-node
smite-node uninstall
sudo rm -rf /opt/smite-node
sudo rm /usr/local/bin/smite-node
```

---

### نکات مهم

1. **فایروال**: مطمئن شوید پورت‌های زیر باز هستند:
   - پنل: پورت 8000 (یا پورتی که در .env تنظیم کرده‌اید)
   - نود: پورت 8888 برای API محلی

2. **IP Forwarding**: برای عملکرد صحیح مش، IP forwarding باید فعال باشد:
   ```bash
   sudo sysctl -w net.ipv4.ip_forward=1
   echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
   ```

3. **لاگ‌ها**: در صورت بروز مشکل، لاگ‌ها را بررسی کنید:
   - پنل: `docker compose logs -f` در `/opt/smite`
   - نود: `smite-node logs` یا `docker compose logs -f` در `/opt/smite-node`

4. **نسخه‌ها**: مطمئن شوید که `SMITE_VERSION` در فایل `.env` روی `main` تنظیم شده است.

---

### پشتیبانی

برای مشکلات و سوالات، لاگ‌ها را بررسی کرده و خطاها را گزارش دهید.
