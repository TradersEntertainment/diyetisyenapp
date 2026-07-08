# 🥗 Diyetisyen — Kişisel Yapay Zeka Beslenme Koçu

Aynı evde yaşayan **iki kişiyi** Telegram üzerinden takip eden, gerçek bir diyetisyen gibi davranan
kişisel yapay zeka asistanı. Her şeyi kalıcı olarak hatırlar, ilerlemeye göre planları uyarlar,
**asla rastgele plan üretmez** — kişinin alışkanlıklarını ve tercihlerini öğrenir.

> **Sadece kişisel kullanım içindir. Ticari kullanım için tasarlanmamıştır.**
> Tıbbi tavsiye yerine geçmez; hastalık/ilaç konularında doktorunuza danışın.

---

## Temel İlke: Protein Sabittir, Diyet Esnektir

Bu asistanın çekirdek mantığı:

- **Protein hedefi değişmezdir.** Her kullanıcı için vücut analizine göre (yağ oranı biliniyorsa yağsız
  vücut kütlesinden, bilinmiyorsa kilo + hedeften) bir **protein tabanı** hesaplanır. Hiçbir plan,
  öneri veya haftalık ayarlama bu tabanın altına inemez. Her tartıda yeniden hesaplanır.
- **Diyet türü sabit değildir.** Dengeli, düşük karbonhidrat, Akdeniz, antrenman odaklı... Strateji,
  kişinin o anki durumuna (hedef, insülin direnci, enerji, açlık, ilerleme) göre yapay zeka tarafından
  seçilir ve gerektiğinde değiştirilir — hep gerekçesiyle. Kalori değişiklikleri daima karbonhidrat ve
  yağdan yapılır, proteinden değil.

---

## Özellikler

- **Detaylı tanışma (onboarding):** 50+ soruluk, hiçbir soru atlanamayan anket — temel bilgiler, sağlık,
  hedefler, egzersiz, beslenme alışkanlıkları, sevdiği/sevmediği/asla yemediği yiyecekler, mutfak
  imkanları, bütçe, alışveriş tercihleri.
- **Doğal dil takibi:** "bugün 84.2'yim, öğlen mercimek çorbası içtim, 2 bardak su" yazın — kilo, öğün
  (kalori/makro tahminiyle), su hepsini otomatik kaydeder.
- **Takip:** kilo, yağ oranı, kas kütlesi, bel/kalça/boyun, su, adım, egzersiz, uyku, ruh hali, stres,
  enerji, açlık, kaçamak, ilerleme fotoğrafları.
- **Kişisel haftalık plan:** 7 gün × 6 öğün (kahvaltı, ara, öğle, ara, akşam, gece); her öğünde kalori,
  makrolar, lif, hazırlık süresi, tarif, alternatifler. Türk & Akdeniz mutfağı ağırlıklı.
- **Ortak alışveriş listesi:** iki kullanıcının planları birleştirilir, kategoriye göre gruplanır,
  Telegram'dan tek dokunuşla "alındı" işaretlenir.
- **Otomatik hesaplar:** BMI, BMR (Mifflin-St Jeor / Katch-McArdle), TDEE, kalori açığı, protein/yağ/
  karbonhidrat/lif/su hedefleri, Navy yağ oranı formülü.
- **İlerleme analizi:** kilo/yağ/kas hızı, plato tespiti, su tutulumu şüphesi, olası fazla/az yeme,
  beslenme/su/egzersiz uyum skorları.
- **Uyarlanabilir diyet (her hafta):** çok hızlı kilo kaybı → kalori artar; durgunluk → ayarlanır;
  kas kaybı → protein artar; düşük enerji → karbonhidrat; aşırı açlık → hacim/lif. Kaçamaklar
  cezalandırılmaz, sonraki günlere yayılır.
- **Günlük Telegram etkileşimi:** günaydın, tartı/su/öğün hatırlatmaları, akşam değerlendirmesi,
  motivasyon, haftalık & aylık rapor, alışveriş hatırlatması.
- **Tam yönetim paneli (web):** grafikler + uyum skorları yanında profil düzenleme, hedefleri manuel
  override etme (protein tabanı burada da korunur — düşük girilirse otomatik tabana yükseltilir),
  tüm kayıtları (kilo/öğün/su/uyku/egzersiz/ruh hali/açlık/vücut analizi) ekleme-düzenleme-silme,
  yiyecek tercihlerini yönetme, haftalık planı görüntüleme ve yeniden oluşturma, ortak alışveriş
  listesini işaretleme, hatırlatma saatlerini düzenleme, kullanıcı ekleme/çıkarma — tek sayfa,
  sekmeli arayüz (token korumalı REST API üzerinde).
- **Docker, günlük yedekleme, loglama.**

---

## Teknoloji

Python 3.12 · FastAPI · PostgreSQL · SQLAlchemy (async) · python-telegram-bot · APScheduler ·
Anthropic Claude · Docker

---

## Kurulum (5 dakika)

### 1. Telegram bot oluşturun
[@BotFather](https://t.me/BotFather)'a `/newbot` yazın, isim verin, **bot token**'ını alın.

### 2. Telegram ID'lerinizi öğrenin
İki kullanıcı da [@userinfobot](https://t.me/userinfobot)'a mesaj atsın; verdiği **sayısal ID**'leri not edin.

### 3. Anthropic API anahtarı alın
[console.anthropic.com](https://console.anthropic.com) → API Keys.

### 4. Ayarları girin
```bash
cp .env.example .env
# .env dosyasını açıp doldurun:
#   TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_IDS (iki ID virgülle), ANTHROPIC_API_KEY, DASHBOARD_TOKEN
```

### 5. Çalıştırın
```bash
docker compose up -d
```
Bu kadar. Veritabanı migration'ları otomatik uygulanır, besin veritabanı tohumlanır, bot çalışmaya başlar.

Telegram'da botunuza **/start** yazarak tanışmaya başlayın.

- Web paneli: `http://localhost:8000/?token=DASHBOARD_TOKEN`
- Sağlık kontrolü: `http://localhost:8000/api/health`

---

## Docker olmadan (geliştirme)

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+asyncpg://diyetisyen:diyetisyen@localhost:5432/diyetisyen"
export TELEGRAM_BOT_TOKEN=... ALLOWED_TELEGRAM_IDS=111,222 ANTHROPIC_API_KEY=... DASHBOARD_TOKEN=...
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Komutlar

| Komut | Açıklama |
|-------|----------|
| `/start` | Tanış / karşılama |
| `/plan` | Bugünün öğünleri (`/plan hafta` tüm hafta) |
| `/kilo 84.5` | Hızlı kilo kaydı |
| `/su 500` | Su kaydı (ml) |
| `/hedef` | Güncel kalori/makro hedefleri |
| `/rapor` | Günlük / haftalık / aylık rapor |
| `/grafik` | İlerleme grafikleri |
| `/alisveris` | Ortak alışveriş listesi |
| `/foto` | İlerleme fotoğrafı |
| `/ayarlar` | Hatırlatma ayarları |
| `/yardim` | Yardım |

Komut dışındaki her mesaj yapay zeka diyetisyene gider.

---

## Yedekleme / Geri Yükleme

Her gece 03:30'da otomatik `pg_dump` yedeği `backups/` klasörüne alınır (son 30 yedek saklanır).
Manuel yedek:
```bash
docker compose exec app sh scripts/backup.sh
```
Geri yükleme:
```bash
gunzip -c backups/diyetisyen_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T db psql -U diyetisyen -d diyetisyen
```

---

## Testler

```bash
pip install pytest pytest-asyncio
pytest            # 66 test — hesaplamalar, protein tabanı invaryantı, uyarlama kuralları,
                  # analiz, alışveriş birleştirme, onboarding kapsamı, AI ajan döngüsü (mock'lu)
```

---

## Proje Yapısı

```
app/
├── main.py            FastAPI + bot + scheduler başlatma
├── config.py, db.py   Ayarlar, async veritabanı
├── models/            SQLAlchemy modelleri
├── services/          calculations (protein tabanı), analysis, adaptive, mealplan,
│                      shopping, reports, targets, seed
├── ai/                client, prompts (TR persona), context, tools, dietitian (ajan)
├── bot/               onboarding, handlers, charts, bot
├── scheduler/jobs.py  hatırlatmalar, haftalık değerlendirme, raporlar, yedek
├── api/               REST rotaları + web paneli
└── data/foods_tr.json Türk/Akdeniz besin veritabanı
```

---

## Yol Haritası (henüz uygulanmadı)

Barkod okuma · yemek fotoğrafı tanıma · yapay zeka kalori tahmini (görselden) · sesli kayıt ·
restoran menü analizi · PDF rapor · Excel dışa aktarma · Google Fit / Apple Health / akıllı saat entegrasyonu.

---

## English Summary

A personal (non-commercial) AI dietitian that coaches **two people in the same household** via Telegram.
It onboards each user with a detailed questionnaire, remembers everything permanently, tracks weight/body
composition/habits, generates personalized (never random) weekly meal plans grounded in learned
preferences, builds a shared shopping list, sends daily reminders, analyzes progress weekly, and adapts
targets over time.

**Core principle:** the **protein target is an invariant anchor** computed from body analysis and never
reduced by any adjustment; the **diet strategy is flexible and AI-driven** (no fixed diet type), chosen
and changed based on the person's current situation. Calorie changes are always absorbed by carbs and fat.

Stack: Python 3.12, FastAPI, PostgreSQL, SQLAlchemy (async), python-telegram-bot, APScheduler, Anthropic
Claude, Docker. Setup: fill `.env` (bot token, two Telegram IDs, Anthropic key, dashboard token) then
`docker compose up -d`. Dashboard at `/?token=...`.
