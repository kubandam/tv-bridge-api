# TV Bridge Monitor - Prehľad

## 📊 URL Dashboard
```
https://tv-bridge-api-ih76.onrender.com/monitor?api_key=BX5SXQVhiiRQxoSCWWqV2pE6M1nBF6Pg
```
 
## ✅ Čo je implementované

### 1. **Štatistiky (horná lišta)**
- ✅ Results (1h) - počet detekcií za poslednú hodinu
- ✅ Ads (1h) - počet reklám detekovaných za poslednú hodinu  
- ✅ Pending - počet čakajúcich príkazov
- ✅ Done - počet dokončených príkazov

### 2. **Raspberry Pi Control Panel**
- ✅ Online/Offline status (zelená/šedá bodka)
- ✅ Last heartbeat (čas posledného kontaktu)
- ✅ Frames captured - celkový počet zachytených snímok
- ✅ Frames processed - počet spracovaných snímok
- ✅ CPU % - využitie procesora
- ✅ Memory % - využitie pamäte
- ✅ Capture/Detect status (running/stopped)
- ✅ Control buttons (Start/Stop/Restart)

### 3. **Live TV Feed**
- ✅ Aktuálny snímok z TV v reálnom čase
- ✅ AD DETECTED / NO AD badge
- ✅ Confidence bar (úroveň istoty detekcie)
- ✅ Auto-refresh každých 2 sekundy

### 4. **Detection Log (Last 10 Images)**
- ✅ Galéria posledných 10 detekovaných snímok
- ✅ Farebné označenie (červená=reklama, zelená=ok)
- ✅ Kliknutím na obrázok sa zobrazí v plnej veľkosti
- ✅ Auto-refresh každých 10 sekúnd

### 5. **Ad Detection Results**
- ✅ List posledných 20 detekcií
- ✅ Čas detekcie
- ✅ AD/Normal status
- ✅ Confidence v percentách
- ✅ Farebné označenie reklám

### 6. **RPi Commands**
- ✅ História príkazov poslaných na Raspberry Pi
- ✅ Status (pending/done/failed)
- ✅ Typ príkazu (start_capture, stop_detect, atď.)
- ✅ Čas spracovania

### 7. **Mobile Commands - NOVÉ! 📱**

#### **Current Mobile Commands (To be executed)**
**Toto je najdôležitejšia sekcia!**
- ✅ Zobrazuje **aktuálne čakajúce príkazy** pre mobilnú aplikáciu
- ✅ Počet pending príkazov v badge
- ✅ Detailné info o každom príkaze:
  - Kanál na ktorý sa má prepnúť
  - Dôvod prepnutia (ad_started / ad_ended)
  - Ako dlho príkaz čaká (s farebným označením)
  - Command ID pre debugging
- ✅ Farebné kódovanie času čakania:
  - 🟢 Zelená: 0-10s (v poriadku)
  - 🟠 Oranžová: 10-30s (pomalá odozva)
  - 🔴 Červená: >30s (problém!)

#### **Mobile Commands History (Last 10)**
- ✅ História posledných 10 príkazov
- ✅ Kompletné info:
  - Kanál
  - Dôvod (ad_started/ad_ended/manual)
  - Status badge (Pending/Done/Failed)
  - Čas vytvorenia a spracovania
- ✅ Vizuálne rozlíšenie statusov

### 8. **Current State**
- ✅ Ad Active (YES/NO)
- ✅ Since (od kedy reklama beží)
- ✅ Fallback CH (náhradný kanál)
- ✅ Original CH (pôvodný kanál)
- ✅ Auto-Switch (enabled/disabled)

## 🔄 Auto-refresh intervals
- Štatistiky a logy: každých **2 sekundy**
- Live image: každých **2 sekundy**
- Image gallery: každých **10 sekúnd**

## 📡 API Endpointy použité

### Monitor Data
```
GET /v1/monitor/data?device_id=tv-1
Headers: X-API-Key: ...

Response: {
  "timestamp": "2026-02-01T...",
  "device_id": "tv-1",
  "rpi_status": { ... },
  "state": { ... },
  "config": { ... },
  "stats": {
    "results_last_hour": 120,
    "ad_detections_last_hour": 15,
    "commands_pending": 2,
    "commands_done": 45,
    "commands_failed": 0
  },
  "recent_results": [...],
  "recent_commands": [...],
  "rpi_commands": [...]
}
```

### Live Image
```
GET /v1/live-image?device_id=tv-1
Headers: X-API-Key, X-Device-Id

Response: {
  "has_image": true,
  "image_base64": "...",
  "timestamp": "...",
  "is_ad": false,
  "confidence": 0.95
}
```

### Image Log
```
GET /v1/rpi/image-log?device_id=tv-1&limit=10&include_images=true
Headers: X-API-Key, X-Device-Id

Response: {
  "device_id": "tv-1",
  "total": 10,
  "items": [
    {
      "index": 0,
      "is_ad": true,
      "confidence": 0.92,
      "captured_at": "...",
      "image_base64": "..."
    },
    ...
  ]
}
```

### RPi Commands
```
POST /v1/rpi/commands
Headers: X-API-Key, X-Device-Id
Body: {
  "type": "start_capture" | "stop_capture" | "start_detect" | "stop_detect" | "restart_all" | "stop_all",
  "payload": {}
}
```

## 🎯 Čo vidíš v monitore

### Pri bežiacom Raspberry Pi:
1. **Zelená bodka** pri "Raspberry Pi Control"
2. **Running** status pre CAPTURE a DETECT
3. **Čísla** v štatistikách (frames, CPU, memory)
4. **Live feed** - aktuálny obraz z TV
5. **Galéria** - posledné detekované snímky
6. **Detection results** - nepretržitý tok detekcií

### Pri vypnutom Raspberry Pi:
1. **Šedá bodka** pri "Raspberry Pi Control"
2. **Stopped** status pre všetko
3. **"-"** v štatistikách
4. **"No Image"** v live feed
5. Prázdna galéria
6. Žiadne nové detection results

### Mobilné príkazy - príklad:
```
📱 Current Mobile Commands (To be executed)
[2 pending]

⏳ 14:23:45  📺 Switch to Channel 3 (ad_started)
           ⏱️ Waiting for 2s - Command ID: 123

⏳ 14:23:30  📺 Switch to Channel 1 (ad_ended)  
           ⏱️ Waiting for 17s - Command ID: 122
           
📱 Mobile Commands History (Last 10)

✓ 14:23:20  📺 Switch to Channel 3 (ad_started)
           ✓ Done - Processed 2s ago

✓ 14:22:50  📺 Switch to Channel 1 (ad_ended)
           ✓ Done - Processed 32s ago
```

## 💡 Tip pre monitoring

**Sleduj hlavne:**
1. **Pending Commands sekciu** - ukazuje čo sa PRÁVE TERAZ má vykonať
2. **Age farbu** - ak je červená (>30s), mobilka nereaguje
3. **Detection Log** - vidíš či RPi detekuje správne
4. **RPi Status** - či vôbec beží

## 🐛 Troubleshooting

### Príkazy zostávajú v pending
- Mobilka nie je pripojená alebo nereaguje
- Polling v mobile app nefunguje
- Skontroluj console logy v mobile app

### Žiadne detection results
- RPi nie je online (šedá bodka)
- Capture/Detect nie je running
- RPi nemá pripojenie k API

### Štatistiky sú prázdne
- Ešte neboli odoslané žiadne dáta
- RPi práve štartuje
- Počkaj aspoň 10-20 sekúnd po štarte

## 📝 Zmeny v API (čo som spravil)

1. ✅ Opravené zobrazenie ad detection results (ikony, formátovanie)
2. ✅ Pridané detailnejšie RPi commands (s časom spracovania)
3. ✅ **NOVÉ:** Sekcia "Current Mobile Commands" - ukazuje pending príkazy
4. ✅ **NOVÉ:** Sekcia "Mobile Commands History" - detailná história
5. ✅ Farebné kódovanie podľa veku príkazu
6. ✅ Zobrazenie dôvodu prepnutia (ad_started/ad_ended)
7. ✅ Command ID pre debugging
8. ✅ Lepšie empty states (keď nie sú žiadne dáta)

Teraz máš v monitore **úplný prehľad** o tom čo sa deje v systéme v reálnom čase!
