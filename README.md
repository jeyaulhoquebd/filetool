# Ubuntu Toolkit — ব্যবহারের বিস্তারিত গাইড

`ubuntu-toolkit` হলো ফাইল সামলানোর সাতটা ছোট টুলের একটা প্যাকেজ। এতে আছে ডাউনলোড ফোল্ডার সাজানো, ডুপ্লিকেট ফাইল খোঁজা, অনেক ফাইলের নাম একসাথে বদলানো, ছবি ছোট করা, আর প্রতিদিন নতুন ওয়ালপেপার বসানো।

> **সব টুলের একটাই নিরাপত্তার নিয়ম:** আগে `--dry-run` দিয়ে দেখুন কী হবে, তারপর আসল কাজ করুন। কোনো টুলই ফাইল স্থায়ীভাবে মুছে ফেলে না।

---

## সূচিপত্র

1. [ইনস্টল ও মুছে ফেলা](#১-ইনস্টল-ও-মুছে-ফেলা)
2. [সাতটা কমান্ড এক নজরে](#২-সাতটা-কমান্ড-এক-নজরে)
3. [download-organizer](#৩-download-organizer--ডাউনলোড-ফোল্ডার-সাজানো)
4. [find-duplicates](#৪-find-duplicates--ডুপ্লিকেট-ফাইল-খোঁজা)
5. [bulk-rename](#৫-bulk-rename--অনেক-ফাইলের-নাম-বদলানো)
6. [image-tool](#৬-image-tool--ছবি-ছোট-ও-কনভার্ট-করা)
7. [filetool](#৭-filetool--রিনেম-ও-ইমেজ-একসাথে)
8. [filetool-gui](#৮-filetool-gui--উইন্ডো-ভার্সন)
9. [bing-wallpaper](#৯-bing-wallpaper--প্রতিদিনের-ওয়ালপেপার)
10. [ব্যাকগ্রাউন্ড সার্ভিস চালানো ও বন্ধ করা](#১০-ব্যাকগ্রাউন্ড-সার্ভিস-চালানো-ও-বন্ধ-করা)
11. [ফাইল কোথায় জমা থাকে](#১১-ফাইল-কোথায়-জমা-থাকে)
12. [সমস্যা হলে](#১২-সমস্যা-হলে)
13. [সংক্ষিপ্ত চিটশিট](#১৩-সংক্ষিপ্ত-চিটশিট)

---

## ১. ইনস্টল ও মুছে ফেলা

### ইনস্টল

`.deb` ফাইলটা যে ফোল্ডারে আছে সেখানে গিয়ে চালান:

```bash
cd ~/deepseek-claude/packaging
sudo apt install ./ubuntu-toolkit_1.0.0_all.deb
```

- ফাইলের নামের আগে `./` অবশ্যই দিতে হবে। না দিলে apt ইন্টারনেটে প্যাকেজ খুঁজতে যাবে।
- ইনস্টলের সময় apt কয়েকটা ডিপেন্ডেন্সি নামাবে (যেমন `python3-watchdog`, `python3-tqdm`, `python3-pyqt6`), তাই ইন্টারনেট লাগবে।
- একই ভার্সন নম্বরের নতুন ফাইল জোর করে বসাতে চাইলে:

```bash
sudo apt install --reinstall ./ubuntu-toolkit_1.0.0_all.deb
```

### ইনস্টল হয়েছে কিনা দেখা

```bash
dpkg -s ubuntu-toolkit | grep -E 'Version|Status'
which filetool bing-wallpaper
```

### মুছে ফেলা

```bash
sudo apt remove ubuntu-toolkit
```

এতে শুধু প্রোগ্রাম মোছে। আপনার সেটিংস, আনডু হিস্টোরি আর ডাউনলোড করা ওয়ালপেপার থেকে যায় (দেখুন [ধাপ ১১](#১১-ফাইল-কোথায়-জমা-থাকে))। ব্যাকগ্রাউন্ডে চলতে থাকা সার্ভিস নিজে বন্ধ হয় না, লগআউট করলে বন্ধ হয়। এখনই থামাতে:

```bash
systemctl --user disable --now download-organizer.service
systemctl --user stop bing-wallpaper.timer
```

---

## ২. সাতটা কমান্ড এক নজরে

| কমান্ড | কাজ |
|---|---|
| `download-organizer` | ডাউনলোড ফোল্ডারের ফাইল টাইপ অনুযায়ী ফোল্ডারে সাজায় |
| `find-duplicates` | একই ফাইলের কপি খুঁজে বের করে, চাইলে Trash-এ পাঠায় |
| `bulk-rename` | অনেক ফাইলের নাম একসাথে বদলায় |
| `image-tool` | ছবি ছোট করে, সাইজ কমায়, ফরম্যাট বদলায় |
| `filetool` | রিনেম আর ইমেজ, দুটোই এক কমান্ডে |
| `filetool-gui` | একই কাজ উইন্ডোতে, প্রিভিউসহ |
| `bing-wallpaper` | Bing-এর আজকের ছবি ওয়ালপেপার করে |

যেকোনো কমান্ডের সব অপশন দেখতে `--help` দিন, যেমন `bulk-rename --help`।

---

## ৩. `download-organizer` — ডাউনলোড ফোল্ডার সাজানো

ছবি, ডকুমেন্ট, ভিডিও, অডিও, আর্কাইভ (জিপ ইত্যাদি) আর প্রোগ্রাম (`.deb`, AppImage ইত্যাদি) আলাদা সাবফোল্ডারে সরিয়ে দেয়। কোন এক্সটেনশন কোন ফোল্ডারে যাবে তা কনফিগ ফাইলে বদলানো যায়।

### প্রথমে দেখুন কী হবে (কিছু সরাবে না)

```bash
download-organizer --dry-run
```

ফোল্ডার না দিলে ডিফল্ট হলো `~/Downloads`। অন্য ফোল্ডারে চালাতে:

```bash
download-organizer ~/test-downloads --dry-run
```

### আসল কাজ

```bash
download-organizer
```

কতগুলো ফাইল সরবে জানিয়ে `Continue? [y/N]` জিজ্ঞেস করবে। `y` দিলে কাজ হবে। প্রশ্ন ছাড়াই চালাতে `-y` দিন:

```bash
download-organizer -y
```

### ভুল হলে ফেরত আনা

```bash
download-organizer --undo
```

এটা সবচেয়ে শেষ রানটা উল্টে দেয়। ফোল্ডারের নাম দিতে হয় না, কারণ আনডু লগ থেকে কাজ করে।

### নতুন ফাইল আসামাত্র সাজানো (watch মোড)

```bash
download-organizer --watch
```

নতুন ফাইল নামানো শেষ হলে (সাইজ কয়েক সেকেন্ড না বদলালে) নিজে সাজিয়ে দেয়। বন্ধ করতে `Ctrl+C`। অপেক্ষার সময় বদলাতে `--settle-seconds 5` দিন।

### গুরুত্বপূর্ণ নিয়ম

- একই নামের ফাইল গন্তব্যে থাকলে পুরনোটা মুছবে না, নতুনটার নাম বদলে `file (1).pdf` করে রাখবে।
- সাবফোল্ডার, লুকানো ফাইল আর অসম্পূর্ণ ডাউনলোড (`.part`, `.crdownload`) ছোঁয়া হয় না।

### অন্যান্য অপশন

| অপশন | কাজ |
|---|---|
| `--config পথ` | অন্য কনফিগ ফাইল ব্যবহার করা |
| `--log-file পথ` | আনডুর লগ অন্য জায়গায় রাখা |

কনফিগ ফাইল প্রথমবার চালালে নিজে তৈরি হয়: `~/.config/download-organizer/config.json`।

### নিজে থেকে চালানো (systemd সার্ভিস)

`download-organizer.service` নামের সার্ভিস `--watch` মোডে ব্যাকগ্রাউন্ডে চলে। এটা ইনস্টলের সময় লগইনে চালু হওয়ার জন্য সেট হয়, কিন্তু এখনই চালু হয় না। এটা আপনার আসল `~/Downloads` ফোল্ডারে কাজ করবে, তাই আগে `--dry-run` দিয়ে ভালোভাবে দেখে নিন।

```bash
# এখনই চালু করা এবং পরের লগইনগুলোতেও চালু রাখা
systemctl --user enable --now download-organizer.service

# চলছে কিনা দেখা
systemctl --user status download-organizer.service

# লগ দেখা
journalctl --user -u download-organizer -n 50

# বন্ধ করা
systemctl --user disable --now download-organizer.service
```

---

## ৪. `find-duplicates` — ডুপ্লিকেট ফাইল খোঁজা

আগে ফাইলের সাইজ মিলিয়ে, তারপর হ্যাশ (SHA-256) মিলিয়ে একই ফাইল খুঁজে বের করে। কতটা জায়গা নষ্ট হচ্ছে তাও দেখায়।

### শুধু খোঁজা (কিছু মুছবে না)

```bash
find-duplicates ~/Downloads ~/Documents
```

একাধিক ফোল্ডার দেওয়া যায়, সবগুলো রিকার্সিভভাবে স্ক্যান হয়।

### ফিল্টার

```bash
# ১ MB-এর ছোট ফাইল বাদ
find-duplicates ~/Downloads --min-size 1MB

# শুধু ছবি
find-duplicates ~/Pictures --ext jpg,png

# কিছু ফোল্ডার বাদ
find-duplicates ~/Projects --exclude node_modules --exclude '*.cache'

# খালি ফাইলসহ সব (ডিফল্টে খালি ফাইল বাদ যায়)
find-duplicates ~/Downloads --min-size 0
```

### রিপোর্ট বানানো

```bash
find-duplicates ~/Downloads --report duplicates.csv
```

CSV ফাইলে প্রতিটি ফাইলের জন্য একটা সারি থাকে। LibreOffice Calc-এ খুলে দেখা যায়।

### ডুপ্লিকেট পরিষ্কার করা

আগে একবার ড্রাই-রানে অভ্যাস করুন। এতে সব প্রশ্ন আসবে, কিন্তু কিছু সরবে না:

```bash
find-duplicates ~/Downloads --cleanup --dry-run
```

তারপর আসল কাজ:

```bash
find-duplicates ~/Downloads --cleanup
```

প্রতিটি ডুপ্লিকেট গ্রুপে নম্বর দেওয়া ফাইল দেখাবে। আপনি কোনটা রাখবেন নির্বাচন করবেন, বাকিগুলো **Trash-এ** যাবে (স্থায়ীভাবে মুছবে না)। ভুল হলে Trash থেকে ফেরত আনা যায়।

### অন্যান্য অপশন

| অপশন | কাজ |
|---|---|
| `--no-progress` | প্রগ্রেস বার লুকানো |

---

## ৫. `bulk-rename` — অনেক ফাইলের নাম বদলানো

### সবসময় প্রথমে `--dry-run`

```bash
bulk-rename ~/Pictures --prefix "trip_" --dry-run
```

আগের নাম ও নতুন নামের টেবিল দেখাবে, কিছু বদলাবে না। ঠিক লাগলে `--dry-run` বাদ দিয়ে চালান। `Proceed?` জিজ্ঞেস করলে `y` দিন।

### সাধারণ উদাহরণ

```bash
# নামের আগে prefix
bulk-rename ~/Pictures --prefix "trip_"

# নামের শেষে suffix (এক্সটেনশনের আগে)
bulk-rename ~/Pictures --suffix "_edited"

# শুধু .jpg ফাইলের নাম photo_001.jpg, photo_002.jpg ... করা
bulk-rename ~/Pictures --pattern "*.jpg" --base photo --number

# নামের ভেতরের লেখা বদলানো
bulk-rename ~/Downloads --replace "IMG_=photo_"

# নামের ভেতরের লেখা মুছে ফেলা ('=' না দিলে লেখাটা মুছে যায়)
bulk-rename ~/Music --replace "Live at "

# এক্সটেনশন ছোট হাতের করা (.JPG → .jpg)
bulk-rename ~/Pictures --lower --replace-scope ext

# সব নাম ছোট / বড় / Title Case
bulk-rename ~/Documents --lower
bulk-rename ~/Documents --upper
bulk-rename ~/Documents --title

# সাবফোল্ডারসহ
bulk-rename ~/Pictures --prefix "2026_" --recursive
```

### নম্বর দেওয়ার অপশন

| অপশন | কাজ |
|---|---|
| `--number` | নম্বর যোগ করা (photo_001, photo_002...) |
| `--number-start 10` | নম্বর কত থেকে শুরু হবে |
| `--number-step 5` | প্রতিবার কতটা বাড়বে |
| `--number-pad 4` | কত ডিজিট (0001, 0002...) |
| `--number-position suffix` | নম্বর নামের শেষে বসবে (ডিফল্ট prefix) |

### কোন ফাইল কোন নম্বর পাবে: `--sort`

নম্বর দিলে ফাইলের ক্রম গুরুত্বপূর্ণ:

```bash
# সবচেয়ে পুরনো ফাইল আগে
bulk-rename ~/Pictures --base trip --number --sort mtime

# সবচেয়ে নতুন আগে
bulk-rename ~/Pictures --base trip --number --sort mtime --reverse
```

`--sort` এর মান: `name`, `mtime` (পরিবর্তনের সময়), `size`, `none`।

### অন্যান্য অপশন

| অপশন | কাজ |
|---|---|
| `--pattern "*.jpg"` | শুধু মিলে যাওয়া ফাইল (একাধিকবার দেওয়া যায়) |
| `--recursive` বা `-r` | সাবফোল্ডারেও কাজ করা |
| `--include-hidden` | `.gitignore` জাতীয় লুকানো ফাইলও রিনেম করা (ডিফল্টে বন্ধ, কারণ এতে সমস্যা হতে পারে) |
| `--replace-scope stem/ext/full` | নামের কোন অংশে কাজ হবে: ফাইলের নাম, এক্সটেনশন, বা পুরোটা |
| `--yes` বা `-y` | `Proceed?` প্রশ্ন বাদ দেওয়া |
| `--log-file পথ` | আনডু লগ অন্য জায়গায় রাখা |

### ভুল হলে ফেরত আনা

```bash
bulk-rename --undo
```

সবচেয়ে শেষ রানের আগের নামগুলো ফিরিয়ে দেয়।

### নাম তৈরির ক্রম

নতুন নাম এই ক্রমে তৈরি হয়: ১) `--replace` ২) `--base` ৩) বড়/ছোট হাতের অক্ষর ৪) `--prefix`/`--suffix` ৫) `--number`।

দুটো ফাইলের নতুন নাম একই হয়ে গেলে, বা নতুন নাম আগে থেকেই আছে এমন হলে টুল **পুরো কাজটাই বাতিল করে দেয়**। কোনো ফাইল ওভাররাইট হয় না।

---

## ৬. `image-tool` — ছবি ছোট ও কনভার্ট করা

**আপনার আসল ছবি কখনো বদলায় না।** ফলাফল সবসময় আলাদা ফোল্ডারে যায়।

### উদাহরণ

```bash
# আগে দেখুন কী হবে
image-tool ~/Pictures --max-width 1200 --dry-run

# ছবি ১৬০০ পিক্সেলের চেয়ে চওড়া হবে না, কোয়ালিটি ৮০
image-tool ~/Pictures --quality 80 --max-width 1600

# সব PNG-কে WebP-তে কনভার্ট (সাবফোল্ডারসহ, ফোল্ডারের কাঠামো একই থাকবে)
image-tool ~/Shots --format webp --recursive

# একটা বক্সের ভেতরে ঢোকানো (চওড়া ও লম্বা দুটো সীমাই)
image-tool ~/Pictures --max-width 1920 --max-height 1080

# আউটপুট ফোল্ডার নিজে ঠিক করা
image-tool ~/Pictures --quality 75 --output ~/Pictures/small
```

### অপশন

| অপশন | কাজ |
|---|---|
| `--quality 1-100` | JPEG ও WebP-এর কোয়ালিটি (ডিফল্ট ৮৫)। কম মানে ছোট সাইজ, কিন্তু কোয়ালিটি কম। PNG লসলেস, তাই এটা মানে না |
| `--max-width PX` | এর চেয়ে চওড়া ছবি ছোট করা (অনুপাত ঠিক থাকে)। ছোট ছবি বদলায় না |
| `--max-height PX` | একই জিনিস উচ্চতার জন্য |
| `--format` | `keep` (ডিফল্ট), `jpeg`, `png`, `webp` |
| `--output ফোল্ডার` | ফলাফল কোথায় যাবে। না দিলে ইনপুট ফোল্ডারের পাশে `<ফোল্ডার>_optimized` নামে তৈরি হয়, যেমন `~/Pictures_optimized` |
| `--recursive` বা `-r` | সাবফোল্ডারও প্রসেস করা |
| `--allow-upscale` | ছোট ছবি বড় করার অনুমতি (ডিফল্টে বন্ধ, কারণ বড় করলে সাইজ বাড়ে, বিস্তারিত বাড়ে না) |
| `--overwrite` | আউটপুট ফোল্ডারে আগের ফাইল থাকলে সেটা বদলানো (না দিলে সেগুলো বাদ যায়) |
| `--dry-run` | কিছু না লিখে শুধু দেখানো |

প্রতিটি ছবির আগের ও পরের সাইজ এবং মোট কতটা জায়গা বাঁচল, শেষে দেখায়।

আউটপুট ফোল্ডার ইনপুট ফোল্ডারের সমান বা ভেতরে হলে টুল চলতে অস্বীকার করে।

---

## ৭. `filetool` — রিনেম ও ইমেজ একসাথে

`filetool` হলো `bulk-rename` আর `image-tool`-এর সম্মিলিত রূপ। দুটো সাবকমান্ড আছে, আর অপশনগুলো আগের দুই টুলের মতোই।

```bash
# রিনেম
filetool rename ~/Pictures --prefix "trip_" --dry-run
filetool rename ~/Pictures --pattern "*.jpg" --base photo --number
filetool rename --undo

# ইমেজ
filetool image ~/Pictures --quality 80 --max-width 1600
filetool image ~/Shots --format webp --recursive
filetool image ~/Pictures --max-width 1200 --dry-run
```

প্রতিটি সাবকমান্ডের সব অপশন দেখতে:

```bash
filetool rename --help
filetool image --help
```

---

## ৮. `filetool-gui` — উইন্ডো ভার্সন

টার্মিনাল ভালো না লাগলে GUI ব্যবহার করুন। রিনেম আর ইমেজের কাজ একই ইঞ্জিনে হয়, আর কিছু লেখার আগে প্রিভিউ টেবিল দেখায়।

**খোলার উপায়:**

- অ্যাপ মেনুতে (`Super` কী চেপে) **Filetool** সার্চ করে ক্লিক করা। এটাই সবচেয়ে নির্ভরযোগ্য উপায়।
- অথবা টার্মিনালে: `filetool-gui`

> কোনো snap অ্যাপের (যেমন snap-এ ইনস্টল করা এডিটরের) ভেতরের টার্মিনাল থেকে খুললে লাইব্রেরির এরর আসতে পারে। দেখুন [ধাপ ১২](#১২-সমস্যা-হলে)।

---

## ৯. `bing-wallpaper` — প্রতিদিনের ওয়ালপেপার

Bing-এর আজকের ছবি নামিয়ে GNOME ডেস্কটপের ব্যাকগ্রাউন্ড হিসেবে বসায়। ছবি জমা হয় `~/Pictures/Wallpapers` ফোল্ডারে।

### হাতে চালানো

```bash
# আজকের ছবি নামিয়ে ওয়ালপেপার করা
bing-wallpaper

# জমানো ছবিগুলোর একটা এলোমেলোভাবে বসানো (ইন্টারনেট ছাড়াই কাজ করে)
bing-wallpaper --random

# শুধু নামানো, ওয়ালপেপার না বদলানো
bing-wallpaper --no-set

# আজকের ছবি আগে থেকে থাকলেও আবার নামানো
bing-wallpaper --force

# ওয়ালপেপার বদলালে নোটিফিকেশন দেখানো
bing-wallpaper --notify
```

### অন্যান্য অপশন

| অপশন | কাজ |
|---|---|
| `--keep 30` | কতগুলো ছবি রাখবে (ডিফল্ট ১৫, এর পুরনোগুলো মুছে যায়) |
| `--dir পথ` | ছবি অন্য ফোল্ডারে রাখা |
| `--market en-US` | Bing-এর কোন দেশের সংস্করণের ছবি (ডিফল্ট `en-US`) |
| `--timeout 30` | প্রতিটি অনুরোধের জন্য কত সেকেন্ড অপেক্ষা |

### প্রতিদিন নিজে থেকে চালানো (টাইমার)

`bing-wallpaper.timer` প্রতিদিন বিকেল ৪:২০-এ এবং লগইনের কিছুক্ষণ পরে ওয়ালপেপার বদলায়।

```bash
# চালু করা
systemctl --user enable --now bing-wallpaper.timer

# পরের রান কখন দেখা
systemctl --user list-timers bing-wallpaper.timer

# লগ দেখা
journalctl --user -u bing-wallpaper -n 50

# বন্ধ করা
systemctl --user disable --now bing-wallpaper.timer
```

> `enable --now` দিলে টাইমার সাথে সাথে একবার ছবি নামিয়ে আপনার ওয়ালপেপার বদলে দেয়। শুধু ভবিষ্যতের জন্য চালু রাখতে চাইলে `--now` বাদ দিন।

সময় বদলাতে চাইলে `systemctl --user edit bing-wallpaper.timer` চালান, অথবা README-তে সময় বদলানোর অংশ দেখুন: `/usr/share/doc/ubuntu-toolkit/README.md`।

---

## ১০. ব্যাকগ্রাউন্ড সার্ভিস চালানো ও বন্ধ করা

প্যাকেজে দুটো ব্যাকগ্রাউন্ড কাজ আছে। ইনস্টলের সময় দুটোই লগইনে চালু হওয়ার জন্য সেট হয়, কিন্তু ইনস্টলের সময় সেগুলো চালু হয় না। নিজে চালু করতে হয়।

| কাজ | কমান্ড |
|---|---|
| ওয়ালপেপার টাইমার চালু | `systemctl --user enable --now bing-wallpaper.timer` |
| ওয়ালপেপার টাইমার বন্ধ | `systemctl --user disable --now bing-wallpaper.timer` |
| ডাউনলোড ওয়াচার চালু | `systemctl --user enable --now download-organizer.service` |
| ডাউনলোড ওয়াচার বন্ধ | `systemctl --user disable --now download-organizer.service` |
| অবস্থা দেখা | `systemctl --user status download-organizer.service` |
| সব টাইমার দেখা | `systemctl --user list-timers` |
| ইউনিট তালিকা | `systemctl --user list-unit-files \| grep -E 'bing\|organizer'` |

### লগইনে যাতে নিজে চালু না হয় (সব ইউজারের জন্য)

```bash
sudo systemctl --global disable download-organizer.service
sudo systemctl --global disable bing-wallpaper.timer
```

---

## ১১. ফাইল কোথায় জমা থাকে

| পথ | কী আছে |
|---|---|
| `~/.config/download-organizer/config.json` | ডাউনলোড অর্গানাইজারের কনফিগ (কোন এক্সটেনশন কোন ফোল্ডারে যাবে) |
| `~/.local/state/download-organizer/history.log` | ডাউনলোড সাজানোর আনডু হিস্টোরি |
| `~/.local/state/bulk-rename/history.log` | রিনেমের আনডু হিস্টোরি |
| `~/Pictures/Wallpapers/` | `bing-wallpaper`-এর নামানো ছবি |
| `~/Downloads/` | `download-organizer` এখানকার ফাইল সাবফোল্ডারে সাজায় |
| `/usr/share/doc/ubuntu-toolkit/README.md` | ইনস্টল হওয়া প্যাকেজের নিজস্ব ডকুমেন্টেশন |

এগুলো প্রথমবার ব্যবহারের সময় নিজে তৈরি হয়। প্যাকেজ মুছলেও এগুলো থেকে যায়। সব মুছে ফেলতে চাইলে হাতে মুছে দিন, তবে আনডু হিস্টোরি মুছলে আর `--undo` কাজ করবে না।

---

## ১২. সমস্যা হলে

### `command not found`

- ইনস্টল হয়েছে কিনা দেখুন: `dpkg -s ubuntu-toolkit`
- ইনস্টলের পর টার্মিনাল বন্ধ করে নতুন করে খুলুন।

### `filetool-gui` খোলে না, `symbol lookup error ... snap/core20` এরর আসে

আপনার টার্মিনাল সম্ভবত কোনো snap অ্যাপের ভেতর থেকে খোলা, আর সেখান থেকে ভুল লাইব্রেরি পথ চলে এসেছে। সমাধান:

1. অ্যাপ মেনু থেকে সাধারণ GNOME Terminal খুলে সেখান থেকে চালান, অথবা মেনু থেকে সরাসরি **Filetool** খুলুন।
2. অথবা এই পরিবেশ ভেরিয়েবল সরিয়ে চালান:

```bash
env -u LD_LIBRARY_PATH filetool-gui
```

### `bulk-rename` কাজ করতে অস্বীকার করছে

দুটো ফাইলের নতুন নাম এক হয়ে যাচ্ছে, অথবা নতুন নামে ফাইল আগে থেকেই আছে। `--dry-run` দিয়ে টেবিল দেখে কোথায় সংঘর্ষ তা বের করুন, তারপর `--base` বা `--number` যোগ করে নামগুলো আলাদা করুন।

### `bing-wallpaper` ছবি নামাতে পারছে না

- ইন্টারনেট আছে কিনা দেখুন।
- ইন্টারনেট ছাড়াও জমানো ছবি থেকে বদলানো যায়: `bing-wallpaper --random`
- সার্ভিসের লগ: `journalctl --user -u bing-wallpaper -n 50`

### ডাউনলোড ওয়াচার কাজ করছে না

```bash
systemctl --user status download-organizer.service
journalctl --user -u download-organizer -n 50
```

চালু না থাকলে [ধাপ ১০](#১০-ব্যাকগ্রাউন্ড-সার্ভিস-চালানো-ও-বন্ধ-করা)-এর কমান্ডে চালু করুন।

### ভুল করে ফাইল সরে গেছে

- ডাউনলোড সাজানোর ভুল: `download-organizer --undo`
- রিনেমের ভুল: `bulk-rename --undo`
- ডুপ্লিকেট ক্লিনআপের ভুল: Trash খুলে ফাইল ফেরত আনুন।

---

## ১৩. সংক্ষিপ্ত চিটশিট

```bash
# ── ডাউনলোড সাজানো ────────────────────────────
download-organizer --dry-run        # আগে দেখুন
download-organizer                  # সাজান
download-organizer --undo           # ফেরত আনুন
download-organizer --watch          # নতুন ফাইল আসামাত্র সাজান

# ── ডুপ্লিকেট ─────────────────────────────────
find-duplicates ~/Downloads --min-size 1MB
find-duplicates ~/Downloads --cleanup --dry-run
find-duplicates ~/Downloads --cleanup

# ── রিনেম ────────────────────────────────────
bulk-rename ~/Pictures --pattern "*.jpg" --base photo --number --dry-run
bulk-rename ~/Pictures --pattern "*.jpg" --base photo --number
bulk-rename --undo

# ── ছবি ──────────────────────────────────────
image-tool ~/Pictures --quality 80 --max-width 1600 --dry-run
image-tool ~/Pictures --quality 80 --max-width 1600
image-tool ~/Shots --format webp --recursive

# ── ওয়ালপেপার ────────────────────────────────
bing-wallpaper
bing-wallpaper --random
systemctl --user enable --now bing-wallpaper.timer

# ── GUI ──────────────────────────────────────
filetool-gui

# ── সাহায্য ──────────────────────────────────
<কমান্ড> --help
```

**মনে রাখার নিয়ম:** নতুন কোনো কাজ আগে `--dry-run` দিয়ে দেখুন, প্রথমে একটা টেস্ট ফোল্ডারে চালান, তারপর আসল ফোল্ডারে যান।