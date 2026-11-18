"""Codici evento dei controller C3/inBio (PullSDK) con descrizione italiana e categoria."""

EVENTS = {
    0: ("Accesso con tessera", "granted"),
    1: ("Tessera durante fascia sempre aperta", "granted"),
    2: ("Prima tessera: apertura normale", "granted"),
    3: ("Apertura multi-tessera", "granted"),
    4: ("Apertura con password di emergenza", "granted"),
    5: ("Apertura in fascia sempre aperta", "info"),
    6: ("Collegamento (linkage) attivato", "alarm"),
    7: ("Allarme annullato", "info"),
    8: ("Apertura remota", "remote"),
    9: ("Chiusura remota", "remote"),
    10: ("Disattivata fascia sempre aperta del giorno", "info"),
    11: ("Attivata fascia sempre aperta del giorno", "info"),
    12: ("Uscita ausiliaria aperta", "info"),
    13: ("Uscita ausiliaria chiusa", "info"),
    14: ("Apertura con impronta", "granted"),
    15: ("Multi-tessera (impronta)", "granted"),
    16: ("Impronta durante fascia sempre aperta", "granted"),
    17: ("Tessera + impronta", "granted"),
    18: ("Prima tessera (impronta)", "granted"),
    19: ("Prima tessera (tessera + impronta)", "granted"),
    20: ("Intervallo tra passaggi troppo breve", "denied"),
    21: ("Porta fuori fascia oraria attiva", "denied"),
    22: ("Fascia oraria non valida", "denied"),
    23: ("Accesso negato", "denied"),
    24: ("Anti-passback", "denied"),
    25: ("Interblocco", "denied"),
    26: ("Autenticazione multi-tessera", "info"),
    27: ("Tessera non registrata", "unknown_card"),
    28: ("Tempo apertura superato", "alarm"),
    29: ("Tessera scaduta", "denied"),
    30: ("Password errata", "denied"),
    31: ("Intervallo impronta troppo breve", "denied"),
    32: ("Autenticazione multi-tessera (impronta)", "info"),
    33: ("Impronta scaduta", "denied"),
    34: ("Impronta non registrata", "denied"),
    35: ("Porta fuori fascia (impronta)", "denied"),
    36: ("Porta fuori fascia (pulsante uscita)", "denied"),
    37: ("Chiusura fallita in fascia sempre aperta", "alarm"),
    41: ("Modalità di verifica non valida", "denied"),
    42: ("Formato Wiegand errato", "denied"),
    101: ("Apertura con password antirapina", "alarm"),
    102: ("Porta aperta in modo anomalo (forzata)", "alarm"),
    103: ("Apertura con impronta antirapina", "alarm"),
    200: ("Porta aperta", "door"),
    201: ("Porta chiusa", "door"),
    202: ("Apertura con pulsante di uscita", "exit"),
    203: ("Multi-tessera (tessera + impronta)", "granted"),
    204: ("Fine fascia sempre aperta", "info"),
    205: ("Apertura permanente remota", "remote"),
    206: ("Avvio dispositivo", "info"),
    208: ("Apertura da superutente", "granted"),
    220: ("Ingresso ausiliario aperto", "aux"),
    221: ("Ingresso ausiliario in corto", "aux"),
}

CATEGORIES = {
    "granted": "Accesso consentito",
    "denied": "Accesso negato",
    "unknown_card": "Tessera sconosciuta",
    "remote": "Comando remoto",
    "exit": "Pulsante uscita",
    "door": "Stato porta",
    "alarm": "Allarme",
    "aux": "Ausiliario",
    "info": "Informazione",
}

VERIFY = {0: "nessuna", 1: "impronta", 3: "password", 4: "tessera", 6: "tessera o impronta",
          10: "tessera + impronta", 11: "tessera + password", 200: "altro"}
INOUT = {0: "entrata", 2: "", 3: "uscita"}

# eventi dopo i quali la serratura è stata attivata per il tempo di apertura
LOCK_RELEASED = {0, 1, 3, 4, 8, 14, 15, 17, 101, 103, 202, 203, 208}
NORMAL_OPEN_START = {2, 5, 18, 19, 205}
NORMAL_OPEN_END = {9, 204}
# eventi in cui event_point è un ingresso/uscita ausiliario anziché una porta
AUX_EVENTS = {12, 13, 220, 221}


def describe(code: int) -> tuple[str, str]:
    return EVENTS.get(code, (f"Evento {code}", "info"))
