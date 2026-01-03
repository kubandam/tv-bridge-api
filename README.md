## TV Bridge API

Jednoduchý FastAPI bridge medzi:
- **Raspberry (AI detekcia reklám)** → posiela live výsledky
- **TV_APP (mobilná appka)** → číta stav (a polluje príkazy)

Auth je momentálne len cez **API key** (header `X-API-Key`). Pairing/session flow je dočasne vypnutý.

### ENV

- **API_KEY**: API key pre všetky requesty
- **DATABASE_URL**: Postgres URL
- **DEFAULT_DEVICE_ID** (optional): fallback device id, ak klient neposiela `X-Device-Id`

Pozri `env.example`.

### Headers

- **X-API-Key**: povinné
- **X-Device-Id**: odporúčané (ak nie je, použije sa `DEFAULT_DEVICE_ID`)

### Endpoints

- **POST** `/v1/ad-results`
  - Body:
    - `is_ad` (bool)
    - `confidence` (float, optional)
    - `captured_at` (ISO datetime, optional)
    - `payload` (object, optional)
  - DB garantuje max **100 posledných** záznamov pre daný `device_id` (ak príde 101., najstarší sa zmaže).

- **GET** `/v1/ad-state`
  - Vráti aktuálny stav odvodený z posledného výsledku (`ad_active`, `ad_since`, ...).

- **GET** `/v1/ad-results?limit=100`
  - Vráti posledné výsledky (default 100) v poradí **od najnovšieho**.

- **GET** `/v1/commands/pull?after_id=0&limit=20`
- **POST** `/v1/commands/{command_id}/ack`
- **POST** `/v1/commands/switch-channel?channel=123`


