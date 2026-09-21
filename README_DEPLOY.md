# Augestel OTP Webhook Bot

Bundle ini berisi source code lengkap dan data runtime dari versi terbaru.

## Prasyarat penting

Augestel hanya menerima endpoint publik `https://`. Aplikasi di dalam bundle
menjalankan listener HTTP pada port lokal/Pterodactyl, sehingga gunakan domain
HTTPS dan reverse proxy (Nginx, Caddy, atau Cloudflare Tunnel) yang meneruskan
request ke port aplikasi.

URL yang dimasukkan ke Augestel harus lengkap:

```text
https://webhook.example.com/webhooks/augestel
```

Jangan memasukkan hanya domain atau IP tanpa path.

## Konfigurasi `.env`

Pertahankan konfigurasi listener berikut:

```env
WEBHOOK_ENABLED=true
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=20319
WEBHOOK_PATH=/webhooks/augestel
WEBHOOK_PUBLIC_URL=https://webhook.example.com
```

Untuk menguji webhook tanpa fallback polling Augestel:

```env
POLLING_ENABLED=false
```

`WEBHOOK_PUBLIC_URL` adalah URL publik dasar. Path `/webhooks/augestel`
ditambahkan oleh aplikasi saat menampilkan endpoint.

## Mode API key / polling saja

Jika hosting hanya menyediakan IP dan port tanpa HTTPS, gunakan mode polling
berbasis API key:

```env
WEBHOOK_ENABLED=false
POLLING_ENABLED=true
```

Interval default bundle ini adalah 20 detik dan mengikuti nilai
`poll_interval` di `bot_settings.json`. Source tidak lagi memaksa jeda 65
detik. Jika ingin memberi jeda tambahan, tambahkan:

```env
API_MIN_REQUEST_GAP_SECONDS=10
```

API key akun dibaca dari `account.json`. Jika Augestel mengembalikan HTTP 429,
cooldown dari server tetap dihormati sampai waktunya selesai.

## Reverse proxy Nginx

Salin `nginx/webhook.conf.example`, ganti `webhook.example.com`, pasang
sertifikat TLS, lalu proxy ke port listener aplikasi:

```text
http://127.0.0.1:20319
```

Jika panel/container tidak memakai localhost, gunakan alamat internal yang
sesuai dengan deployment.

## Menjalankan

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

Tes health endpoint:

```bash
curl -fsS https://webhook.example.com/webhooks/augestel/health
```

Setelah endpoint HTTPS aktif, masukkan URL lengkap tersebut di halaman
Augestel, klik **Update endpoint**, lalu aktifkan **Deliver events to this
endpoint**.

## Catatan keamanan

File `.env` dan `account.json` berisi credential runtime dan harus tetap
memiliki permission privat. Jangan commit atau membagikan keduanya ke publik.