# ZKTeco Access – add-on per Home Assistant

<img src="zkteco_access/logo.png" width="320">

Gestione completa dei controller di accesso **ZKTeco C3 / inBio** direttamente da
Home Assistant, senza il PC con ZKAccess 3.5: apertura porte, tessere RFID su una o più
porte, livelli, fasce orarie, eventi in tempo reale, entità e automazioni.

## Installazione

**Da repository GitHub**: Impostazioni → Componenti aggiuntivi → Store → ⋮ → Repository →
aggiungi `https://github.com/stepsolar/ha-zkteco-access` → installa **ZKTeco Access**.

**Come add-on locale**: copia la cartella `zkteco_access` in `/addons/` (condivisione Samba
`addons`), poi Store → ⋮ → *Verifica aggiornamenti* → sezione *Add-on locali*.

Documentazione completa: [zkteco_access/DOCS.md](zkteco_access/DOCS.md).

## Integrazione Home Assistant (porte, serrature, sensori)

Nella cartella [`custom_components/zkteco_access`](custom_components/zkteco_access) c'è l'integrazione
**ZKTeco Access**, simile a [hass-zkaccess_c3](https://github.com/vwout/hass-zkaccess_c3) ma **dipendente
dall'add-on**: non apre connessioni verso i controller, manda ogni comando all'add-on. Così c'è un solo
programma che parla con i controller e non ci sono conflitti su eventi e tessere.

Installazione con **HACS**: HACS → ⋮ → Repository personalizzati → `https://github.com/stepsolar/ha-zkteco-access`,
categoria *Integrazione* → scarica **ZKTeco Access** → riavvia Home Assistant. Con l'add-on avviato,
in Impostazioni → Dispositivi e servizi compare **"ZKTeco Access rilevato"**: premi *Configura*.
(Manuale: copia `custom_components/zkteco_access` in `/config/custom_components/`.)

Entità create per ogni porta: serratura (`lock`, sblocca/apri = impulso, blocca = richiude), pulsante
*Apri*, interruttore *sempre aperta*, sensore porta, allarme, ultimo evento, entità evento per le automazioni.
Per ogni controller: connessione, annulla allarmi, sincronizza ora, riavvia; uscite ausiliarie come
interruttori, ingressi ausiliari come sensori. Servizi: `zkteco_access.grant_card`, `revoke_card`,
`enable_card`, `disable_card`, `learn_card` (restituisce il numero letto), `sync_all`.

## Sviluppo

```
python -m pytest tests          # protocollo, sincronizzazione, MQTT (con controller simulati)
python tests/sim_panel.py 4370  # simulatore di un C3-400 per provare l'interfaccia
```

Il formato dei comandi di scrittura/cancellazione tabelle (0x07/0x09) è stato ricavato
dal PullSDK `plcommpro.dll`; il fixture `tests/fixtures/c3_table_cfg.hex` è una risposta
reale di un C3-400 tratta dai test di [zkaccess-c3-py](https://github.com/vwout/zkaccess-c3-py).
