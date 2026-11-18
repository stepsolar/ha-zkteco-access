# ZKTeco Access

Add-on che sostituisce **ZKAccess 3.5** sul PC: Home Assistant parla direttamente con i
controller ZKTeco **C3-100/200/400** e **inBio** tramite il loro protocollo di rete
(lo stesso del PullSDK `plcommpro.dll`), senza DLL, senza Wine e senza PC acceso.

## Funzioni

| Funzione di ZKAccess 3.5 | Nell'add-on |
|---|---|
| Dispositivi (IP, porta, password di comunicazione) | Scheda **Dispositivi**: aggiungi, prova connessione, info, ora, riavvio |
| Monitoraggio in tempo reale | Scheda **Monitor**: stato di ogni porta (serratura, sensore, allarme), ultimo accesso, feed eventi live |
| Apertura / chiusura remota, "sempre aperta" | Pulsanti **Apri** / **Sempre aperta** e entità Home Assistant |
| Annulla allarme, riavvio controller, sincronizza ora | Pulsanti per controller e globali |
| Personale / tessere (n. tessera, password, validità) | Scheda **Tessere**: crea, modifica, disattiva, scadenza, ricerca, azioni multiple |
| Registrazione tessera dal lettore | Pulsante **Leggi dal lettore**: avvicini la tessera a un qualsiasi lettore e il numero si compila da solo |
| Livelli di accesso (porte + fascia oraria) | Scheda **Livelli**; ogni tessera può avere più livelli **e** porte singole |
| Fasce orarie (3 intervalli/giorno + 3 tipi festivi) | Scheda **Fasce orarie** con invio ai controller |
| Festività | Scheda **Fasce orarie** → Festività |
| Parametri porta (tempo apertura, sensore, verifica, super password, password antirapina, intervallo letture, fascia sempre aperta) | Scheda **Porte** → **Parametri controller** (letti e scritti sul controller) |
| Ingressi/uscite ausiliarie | Scheda **Porte**: rinomina, attiva uscite; entità HA |
| Report eventi | Scheda **Eventi**: filtri, ricerca, esportazione CSV |
| Download tessere dal controller | **Tessere nel controller** (sola lettura) e **Importa tessere dai controller** |
| Sincronizzazione dati sui controller | Automatica ad ogni modifica; se un controller è offline la modifica resta **in coda** e parte appena torna online. **Sincronizza tutti** riscrive tutto |
| Database ZKAccess | Import diretto di `zkaccess.mdb` (controller, password, porte, ausiliari, fasce, livelli, 124 tessere con camere e livelli) |

Le regole di **linkage**, **interblocco**, **anti-passback** e i formati Wiegand restano
memorizzati nei controller: l'add-on non li cancella. I moduli presenza (ZKTime/Att3000),
le mappe grafiche e le impronte digitali non sono gestiti.

## Primo avvio (migrazione da ZKAccess)

1. Copia `C:\ZKTeco\ZKAccess3.5\zkaccess.mdb` in **/share/zkaccess.mdb** (cartella *share* di
   Samba) oppure caricalo da **Impostazioni → Importa da ZKAccess 3.5**.
   Al primo avvio, se il file esiste in `/share`, viene importato automaticamente.
2. Su **Dispositivi** controlla che i controller risultino online (pallino verde).
3. Apri **Tessere nel controller** su un paio di controller e confronta con la scheda **Tessere**.
4. **Chiudi ZKAccess sul PC** (almeno il Monitoraggio): i controller consegnano ogni evento
   a un solo programma, e due programmi che scrivono le tessere si sovrascrivono.
5. Prova con una tessera: modificala, premi **Salva e invia**, verifica sul lettore.
6. Solo dopo, se vuoi riallineare tutto, usa **Sincronizza tutti** (le tessere presenti nei
   controller ma non nell'add-on vengono rimosse).

## Integrazione ZKTeco Access (consigliata)

Installa da HACS l'integrazione **ZKTeco Access** (stesso repository GitHub, categoria *Integrazione*).
L'add-on si annuncia da solo: in Impostazioni → Dispositivi e servizi compare "ZKTeco Access rilevato".
L'integrazione passa sempre dall'add-on, quindi non entra mai in conflitto con lui.

Crea per ogni porta: serratura, pulsante Apri, interruttore "sempre aperta", sensore porta, allarme,
ultimo evento ed entità evento; per ogni controller: connessione, annulla allarmi, sincronizza ora,
riavvia; uscite/ingressi ausiliari. Servizi per le automazioni:

```yaml
service: zkteco_access.grant_card      # check-in
data:
  card: "2095868724"
  name: Rossi
  group: "101"
  levels: ["101", "Cancello"]
  end_date: "2026-11-21"
---
service: zkteco_access.disable_card    # check-out
data:
  card: "2095868724"
---
service: zkteco_access.learn_card      # restituisce il numero della prossima tessera letta
data:
  timeout: 45
response_variable: tessera
```

Altri servizi: `revoke_card`, `enable_card`, `sync_all`.

## Entità via MQTT (alternativa)

Se non usi l'integrazione puoi attivare l'opzione `mqtt_enabled` dell'add-on (serve il broker
**Mosquitto**). Non attivarla insieme all'integrazione, altrimenti avrai le entità doppie; quando la
disattivi, le entità MQTT create in precedenza vengono rimosse. Per ogni porta:

- `lock.*` serratura: *Sblocca/Apri* = impulso di apertura, *Blocca* = chiude subito
- `button.* apri`, `switch.* sempre aperta`
- `binary_sensor.* sensore` (porta aperta/chiusa) e `binary_sensor.* allarme`
- `sensor.* ultimo evento` (con attributi: utente, tessera, PIN, verifica, entrata/uscita)
- `event.* accessi` con tipi: `granted`, `denied`, `unknown_card`, `remote`, `exit`, `door`, `alarm`, `aux`, `info`

Per ogni controller: connessione, riavvio, annulla allarmi, sincronizza ora, sincronizza
utenti, modifiche in coda. In più `sensor.zk_last_card` (ultima tessera letta) e
`event.zk_access_events` (tutti gli eventi). Gli ID entità delle porte sono del tipo
`lock.zk_door12_lock`, `event.zk_door12_event`… (rinominabili da Home Assistant).

### Esempi di automazioni

Notifica quando una tessera sconosciuta viene passata:

```yaml
trigger:
  - platform: state
    entity_id: event.zk_access_events
action:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'unknown_card' }}"
  - service: notify.mobile_app_telefono
    data:
      message: "Tessera sconosciuta {{ trigger.to_state.attributes.card }} su {{ trigger.to_state.attributes.door }}"
```

Check-in: abilita una tessera per camera 101 + cancello fino al giorno di partenza:

```yaml
service: mqtt.publish
data:
  topic: zkteco_access/cmd
  payload: >
    {"action":"grant","card":"2095868724","name":"Rossi","grp":"101",
     "levels":["101","Cancello"],"start_date":"2026-11-18","end_date":"2026-11-21"}
```

Altre azioni su `zkteco_access/cmd` (esito su `zkteco_access/cmd/result`):

| action | parametri |
|---|---|
| `open` / `close` | `door` (nome o id), `seconds` |
| `normal_open` | `door`, `enable` |
| `grant` | `card` e/o `pin`, `name`, `grp`, `levels`, `doors`, `start_date`, `end_date`, `add` (aggiunge invece di sostituire) |
| `revoke` | `card` o `pin` |
| `disable` / `enable` | `card` o `pin` |
| `cancel_alarm` / `restart` / `sync_time` | `panel_id` facoltativo |
| `sync_all` | `remove_unknown` |
| `learn` | `timeout` (restituisce la prossima tessera letta) |

Le tessere con data di fine passata vengono disattivate automaticamente (opzione
`auto_disable_expired`); in ogni caso il controller stesso rifiuta la tessera dopo la scadenza.

## Opzioni

| Opzione | Descrizione |
|---|---|
| `poll_interval` | secondi tra una lettura eventi e l'altra (default 1) |
| `timeout` | timeout di rete verso i controller |
| `sync_time_on_connect` | imposta l'ora dei controller a ogni connessione |
| `import_path` | database ZKAccess importato al primo avvio |
| `mqtt_*` | broker esterno (lasciare vuoto per Mosquitto) |
| `api_token` | abilita l'API REST sulla porta 8099 (header `Authorization: Bearer <token>`) |

## Note tecniche

- Protocollo: frame `AA 01 cmd len … crc16 55`; tabelle `user`, `userauthorize`, `timezone`,
  `holiday` lette dalla struttura dichiarata da ogni controller (firmware 4.x e 5.x).
- Ogni tessera viene scritta come `user` (Pin, CardNo, Password, StartTime, EndTime) +
  `userauthorize` (Pin, fascia, maschera porte 1/2/4/8) su ogni controller che ha almeno
  una delle sue porte; dai controller dove non ha più porte viene cancellata.
- Database dell'add-on: `/data/zkteco.db` (incluso nei backup di Home Assistant).
