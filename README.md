# هسته فروشگاه تلگرامی قابل تنظیم

این مخزن یک **Modular Monolith** برای فروش امن محصولات دیجیتال است. نصب اولیه عمداً هیچ
دسته، محصول، قیمت، قوانین یا نام تجاری ایجاد نمی‌کند. تمام محتوا باید پس از نصب تنظیم شود.

## مرز امنیتی و قواعد قطعی

- مشاهده کاتالوگ بدون KYC مجاز است، اما Checkout در سرویس دامنه دوباره Consent آخرین قوانین،
  `VERIFIED` بودن KYC، مالکیت کارت تأییدشده و Risk را کنترل می‌کند.
- PAN کارت مقصد رمزنگاری و فقط پس از Final Check معتبر به‌شکل پویا Decrypt می‌شود؛ Callback،
  متن پایدار، Log و اعلان فقط شناسه یا Masked PAN دارند.
- Quote بر ساعت UTC دقیقاً ۳۰ دقیقه معتبر و قیمت/نرخ آن Snapshot است. انقضا Order را
  `PAYMENT_EXPIRED` می‌کند و رزرو موجودی را آزاد می‌کند.
- رسید پرداخت فقط `AWAITING_RECONCILIATION` می‌سازد. فقط تأیید Server-to-Server یا بررسی
  واقعی مجاز است Payment را `VERIFIED` و Order را `READY_FOR_FULFILLMENT` کند.
- CVV، PIN، رمز ایستا/پویا و OTP نه در فرم و نه در مدل دریافت نمی‌شوند.
- Wallet یک Ledger افزایشی و تغییرناپذیر دارد. Wallet، Referral، Cooperation و Membership
  به‌طور پیش‌فرض خاموش‌اند.

## اجزا

`domain.py` منطق Users، Terms/Consent، KYC، Cards، Risk، Catalog/Pricing/Quotes، Checkout،
Orders، Payments/Reconciliation، Ledger و Fulfillment را نگه می‌دارد. `providers.py` مرزهای
KYC/Payment/Currency رسمی را تعریف می‌کند؛ هیچ API بانکی غیررسمی استفاده نشده است.
`telegram_adapter.py` فیلدهای رسمی `style` و `icon_custom_emoji_id` و Offset UTF-16 را مستقیم
به Bot API می‌فرستد. FastAPI Health/Webhook host، PostgreSQL، Redis و Jobهای دوره‌ای در یک
واحد استقرار باقی می‌مانند و Microservice نیستند.

## نصب

نیازمندی: Docker و Docker Compose.

```bash
git clone REPOSITORY_URL
cd REPOSITORY_NAME
bash install.sh
```

Installer توکن را مخفی دریافت و با `getMe` اعتبارسنجی می‌کند، Secret تصادفی می‌سازد، `.env`
با مجوز `600` می‌نویسد، Migration را اجرا و Health Check می‌کند. سپس در تلگرام `/admin` را
ارسال کنید. برای یافتن شناسه ادمین می‌توان ابتدا به ربات `/start` فرستاد و Update امن را با
ابزار رسمی Telegram بررسی کرد؛ توکن نباید در Shell history یا پیام پشتیبانی قرار گیرد.

## مدیریت

### نرخ ارز تأمین‌کننده

هزینه هر محصول با مبلغ Decimal و کد ISO-4217 ذخیره می‌شود و قیمت نهایی همچنان یک عدد صحیح
تومان است. حالت دستی بدون کلید API کار می‌کند. برای دریافت دوره‌ای نرخ‌ها از Navasan،
`FX_PROVIDER=navasan` و `NAVASAN_API_KEY` را فقط در فایل محرمانه `.env` تنظیم کنید. فاصله
پیش‌فرض دریافت ۳۶۰ دقیقه است تا با سقف ۱۲۰ درخواست ماهانه سازگار باشد. Checkout هرگز API
را مستقیم صدا نمی‌زند و فقط آخرین نرخ معتبر PostgreSQL را مصرف می‌کند؛ نرخ API منقضی،
Quote جدید همان ارز را متوقف می‌کند ولی Quoteهای ۳۰ دقیقه‌ای قبلی تغییر نمی‌کنند.

```bash
bash manage.sh status
bash manage.sh logs
bash manage.sh migrate
bash manage.sh backup
bash manage.sh restore backups/FILE.sql.gz
bash manage.sh doctor
```

Backup شامل داده حساس است؛ فایل خروجی را بلافاصله با ابزار سازمانی و کلید خارج از سرور
رمزنگاری کنید. Key envelope دارای شناسه نسخه است و `Vault.rotate` چرخش کلید را پشتیبانی می‌کند.

## توسعه و آزمون

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest --cov --cov-report=term-missing --cov-fail-under=80
alembic upgrade head
```

تست‌ها جریان Rules → KYC → Card ownership → Quote → Final Check → Payment/Reconciliation →
Notification → Claim را بدون پرداخت واقعی اجرا می‌کنند. Provider واقعی باید مجوز قانونی،
تطبیق کارت و Verification سمت سرور داشته باشد؛ تا آن زمان Strong Match خودکار ادعا نمی‌شود.

## Telegram Premium Emoji

Bootstrap با متن ساده است. آیکون اختیاری فقط `icon_custom_emoji_id` دارد و هیچ Emoji یونیکد
تزئینی به‌عنوان fallback تولید نمی‌شود. Registry باید ID را از Entity نوع `custom_emoji`
استخراج و Test Send کند. Offset متن با UTF-16 محاسبه می‌شود. Button style یکی از default،
primary، success یا danger است.

## عملیات Production

- TLS و Reverse Proxy را جلوی پورت محلی 8080 قرار دهید و `RUN_MODE=webhook` تنظیم کنید.
- مدارک را خارج Web Root و با لینک کوتاه‌عمر نگهدارید؛ مشاهده باید RBAC و Audit شود.
- PostgreSQL backup، Redis persistence، مانیتور Health، Rate Limit و Job انقضای Quote را فعال کنید.
- هیچ Provider بانکی در این مخزن تأیید یا متصل نشده است؛ Adapterها عمداً Interface هستند.

## وضعیت راستی‌آزمایی

این نسخه تا زمانی که Workflow رسمی CI سبز نشده و Migration و Docker روی زیرساخت دارای Docker
اجرا نشده‌اند **Production-ready نیست**. مسیر Runtime از `ShopRepository` تراکنشی، PostgreSQL،
Redis و Outbox پایدار استفاده می‌کند. پیاده‌سازی قدیمی `ApplicationStore` حذف شده و تنها مسیر
تجاری برنامه `main.py → runtime.py → ShopRepository` است. Migration دوم موجودیت‌های MVP و فیلدهای تجاری را
اضافه می‌کند. اجرای واقعی Migration در محیط فعلی به‌دلیل نبود Docker هنوز تأیید نشده است.
بدون Provider رسمی، Strong Card Match و `allowed_card` غیرفعال‌اند، کارت‌به‌کارت فقط Manual
Reconciliation است و تصویر رسید هرگز اثبات پرداخت محسوب نمی‌شود.

در MVP فعلی، `file_id` و `file_unique_id` تلگرام مرجع اصلی مدارک KYC، مالکیت کارت و رسید
هستند. دانلود و آرشیو محلی رمزنگاری‌شده هنوز فعال نشده است و نباید ادعا شود. مرز
`EvidenceStorage` برای افزودن Storage رمزنگاری‌شده محلی یا S3-compatible در آینده تعریف شده؛
هیچ مدرکی زیر Web Root عمومی نوشته نمی‌شود و محتوای فایل وارد Log یا Audit نمی‌گردد.

Polling هم‌زمان Health server را روی پورت 8080 اجرا می‌کند. Webhook فقط روی مسیر ثابت
`/telegram/webhook`، با بررسی Header محرمانه Telegram، Update را به Dispatcher می‌دهد. Readiness
واقعاً PostgreSQL و Redis را Probe می‌کند و در قطع وابستگی‌ها HTTP 503 برمی‌گرداند.
