# Changelog

## 1.1.1
- L'add-on accetta le richieste da tutta la rete interna di Home Assistant (172.30.32.0/23) e registra nel log quelle rifiutate.
- Integrazione: trova da sola il nome host dell'add-on installato da GitHub e mostra il motivo preciso dell'errore (host non trovato, connessione rifiutata, accesso negato).

## 1.1.0
- Nuova integrazione Home Assistant **ZKTeco Access** (HACS, cartella custom_components) che usa l'add-on come unico punto di accesso ai controller: serrature, pulsanti, sensori, eventi e servizi tessere.
- L'add-on si annuncia a Home Assistant (scoperta automatica dell'integrazione) e accetta le richieste di Home Assistant.
- Entità MQTT ora disattivate di default (`mqtt_enabled`); disattivandole vengono rimosse quelle già create, per evitare doppioni con l'integrazione.

## 1.0.2
- Interfaccia ottimizzata per smartphone: menu a scorrimento, tabelle trasformate in schede, finestre a tutto schermo, pulsanti più grandi, nessuno zoom automatico su iPhone.

## 1.0.1
- Import di zkaccess.mdb dal browser inviato a pezzi da 4 MB: risolve l'errore 413 del proxy ingress di Home Assistant.

## 1.0.0
- Prima versione: protocollo nativo C3/inBio (connessione con/senza sessione e password),
  monitor in tempo reale, apertura/chiusura/sempre aperta, uscite ausiliarie,
  tessere con livelli e porte singole, lettura tessera dal lettore, validità,
  fasce orarie e festività, parametri porta, eventi con CSV, import di zkaccess.mdb,
  coda offline, sincronizzazione completa, entità Home Assistant via MQTT discovery,
  comandi MQTT/REST per automazioni (check-in/check-out).
