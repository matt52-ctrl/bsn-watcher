#!/usr/bin/env python3
"""Sorveglia gli slot dell'appuntamento BSN al Comune di Utrecht (sistema Qmatic).

  python3 bsn.py --once    un solo controllo, poi esce   (usato da GitHub Actions)
  python3 bsn.py           ciclo continuo ogni 60s       (usato in locale)
"""

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime

BASE = "https://afspraak.utrecht.nl/qmaticwebbooking/rest/schedule"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BRANCH_INTERNAL_ID = 3          # Publiekszaken, Stadsplateau 1
SERVIZI_INTERNAL_ID = [166]     # Internationale studentenregistratie
                                # 147 = Utrecht International Center, 283 = Combi UIC

APPUNTAMENTO_ATTUALE = "2026-10-02 13:00"

INTERVALLO_SEC = 60
CIECO_PRIMA_DI_AVVISARE = 10
HEARTBEAT_ORE = 24
PRENOTA_URL = "https://afspraak.utrecht.nl/qmaticwebbooking/"

QUI = os.path.dirname(os.path.abspath(__file__))
STATO_FILE = os.path.join(QUI, "stato.json")
LOG_FILE = os.path.join(QUI, "watcher.log")
SU_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    riga = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(riga, flush=True)
    if not SU_ACTIONS:                     # su Actions il log e' quello della run
        with open(LOG_FILE, "a") as f:
            f.write(riga + "\n")


def _carica_env():
    """Le variabili d'ambiente vincono sul file .env (su Actions arrivano dai Secrets)."""
    valori = {}
    percorso = os.path.join(QUI, ".env")
    if os.path.exists(percorso):
        with open(percorso) as f:
            for riga in f:
                riga = riga.strip()
                if riga and not riga.startswith("#") and "=" in riga:
                    k, v = riga.split("=", 1)
                    valori[k.strip()] = v.strip()
    for chiave in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(chiave):
            valori[chiave] = os.environ[chiave]
    return valori


ENV = _carica_env()
TOKEN = ENV.get("TELEGRAM_TOKEN", "")
CHAT_ID = ENV.get("TELEGRAM_CHAT_ID", "")


# Il Python di sistema del Mac (3.9 / LibreSSL 2.8.3) viene rifiutato dal server con
# TLSV1_ALERT_PROTOCOL_VERSION, quindi la rete passa da curl.
def _curl(args, timeout):
    try:
        p = subprocess.run(["curl", "-sS", "--max-time", str(timeout)] + args,
                           capture_output=True, text=True, timeout=timeout + 10)
        return p.returncode, p.stdout, p.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def telegram(testo, silenzioso=False):
    if not TOKEN or not CHAT_ID:
        log("!! Telegram non configurato, messaggio non inviato")
        return False
    args = ["-X", "POST", "https://api.telegram.org/bot%s/sendMessage" % TOKEN,
            "--data-urlencode", "chat_id=" + CHAT_ID,
            "--data-urlencode", "text=" + testo,
            "--data-urlencode", "parse_mode=HTML",
            "--data-urlencode", "disable_web_page_preview=true",
            "--data-urlencode", "disable_notification=" + ("true" if silenzioso else "false")]
    for tentativo in range(3):
        codice, out, _ = _curl(args, 20)
        if codice == 0:
            try:
                if json.loads(out).get("ok"):
                    return True
            except ValueError:
                pass
        log("!! Telegram tentativo %d fallito" % (tentativo + 1))   # mai stampare il token
        time.sleep(3)
    return False


def get_json(url):
    """L'oggetto letto, oppure None se non siamo riusciti a leggere.

    None significa 'sono cieco', NON 'non c'e' niente': i chiamanti non devono
    mai appiattire i due casi con un `or []`.
    """
    codice, out, err = _curl(["-A", UA, "-H", "Accept: application/json",
                              "-w", "\n%{http_code}", url], 25)
    breve = url.split("/rest/")[-1][:70]
    if codice != 0:
        log("!! rete ko su %s: %s" % (breve, err[:120]))
        return None
    corpo, _, stato = out.rpartition("\n")
    if stato.strip() != "200":
        log("!! HTTP %s su %s" % (stato.strip(), breve))
        return None
    try:
        return json.loads(corpo)
    except ValueError:
        log("!! risposta non JSON su %s" % breve)
        return None


def risolvi_id():
    """branch id + {internalId: (publicId, nome)}, riletti dal sito e non fissati a mano."""
    rami = get_json(BASE + "/branches")
    if not rami:
        return None, None
    scelti = [b for b in rami if b.get("internalId") == BRANCH_INTERNAL_ID]
    if not scelti:
        log("!! branch %s non trovato" % BRANCH_INTERNAL_ID)
        return None, None
    branch = scelti[0]["id"]
    servizi = get_json("%s/branches/%s/services" % (BASE, branch))
    if not servizi:
        return branch, None
    mappa = {}
    for s in servizi:
        if s.get("internalId") in SERVIZI_INTERNAL_ID:
            mappa[s["internalId"]] = (s["publicId"], s.get("name", "?"))
    mancanti = [i for i in SERVIZI_INTERNAL_ID if i not in mappa]
    if mancanti:
        log("!! servizi spariti dal listino: %s" % mancanti)
    return branch, mappa


def slot_migliori(branch, service_id, limite):
    """Slot strettamente precedenti a `limite`. None = lettura fallita."""
    date = get_json("%s/branches/%s/dates;servicePublicId=%s/" % (BASE, branch, service_id))
    if date is None:
        return None
    trovati = []
    for voce in date:
        giorno = voce.get("date")
        if not giorno:
            continue
        try:
            g = datetime.strptime(giorno, "%Y-%m-%d").date()
        except ValueError:
            continue
        if g > limite.date():
            continue
        orari = get_json("%s/branches/%s/dates/%s/times;servicePublicId=%s/"
                         % (BASE, branch, giorno, service_id))
        if orari is None:
            return None
        for o in orari:
            ora = o.get("time")
            if not ora:
                continue
            try:
                quando = datetime.strptime("%s %s" % (giorno, ora), "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if quando < limite:
                trovati.append(quando)
    return sorted(trovati)


def carica_stato():
    if os.path.exists(STATO_FILE):
        try:
            with open(STATO_FILE) as f:
                s = json.load(f)
                s.setdefault("visti", [])
                s.setdefault("ultimo_heartbeat", 0)
                s.setdefault("cicli_ciechi", 0)
                s.setdefault("cecita_segnalata", False)
                return s
        except Exception:
            pass
    return {"visti": [], "ultimo_heartbeat": 0, "cicli_ciechi": 0, "cecita_segnalata": False}


def salva_stato(stato):
    with open(STATO_FILE, "w") as f:
        json.dump(stato, f, indent=1, sort_keys=True)
        f.write("\n")


def un_controllo(stato):
    """Un singolo giro completo. Aggiorna `stato` e manda gli avvisi del caso."""
    limite = datetime.strptime(APPUNTAMENTO_ATTUALE, "%Y-%m-%d %H:%M")
    visti = set(stato["visti"])
    branch, servizi = risolvi_id()

    cieco = False
    novita = []
    totale = 0

    if not branch or not servizi:
        cieco = True
    else:
        for internal_id, (public_id, nome) in servizi.items():
            slot = slot_migliori(branch, public_id, limite)
            if slot is None:
                cieco = True
                continue
            totale += len(slot)
            for quando in slot:
                chiave = "%s|%s" % (internal_id, quando.strftime("%Y-%m-%d %H:%M"))
                if chiave not in visti:
                    visti.add(chiave)
                    novita.append((nome, quando))

    if novita:
        righe = ["<b>Slot BSN piu' presto disponibile</b>", ""]
        for nome, quando in sorted(novita, key=lambda x: x[1]):
            righe.append("%s  ore %s  (%s)"
                         % (quando.strftime("%a %d/%m"), quando.strftime("%H:%M"), nome))
        righe += ["", "Il tuo attuale: %s" % APPUNTAMENTO_ATTUALE, PRENOTA_URL,
                  "", "Prenota tu, poi disdici il vecchio."]
        telegram("\n".join(righe))
        log(">>> %d nuovi slot notificati" % len(novita))

    if cieco:
        stato["cicli_ciechi"] += 1
        if stato["cicli_ciechi"] >= CIECO_PRIMA_DI_AVVISARE and not stato["cecita_segnalata"]:
            telegram("Watcher BSN: non riesco a leggere il sito da %d controlli. "
                     "Il silenzio adesso non vuol dire 'niente slot'." % stato["cicli_ciechi"])
            stato["cecita_segnalata"] = True
    else:
        if stato["cecita_segnalata"]:
            telegram("Watcher BSN: sito di nuovo leggibile.", silenzioso=True)
        stato["cicli_ciechi"] = 0
        stato["cecita_segnalata"] = False

    if time.time() - stato["ultimo_heartbeat"] > HEARTBEAT_ORE * 3600:
        stato["ultimo_heartbeat"] = time.time()
        telegram("Watcher BSN vivo. Nessuno slot prima del %s." % APPUNTAMENTO_ATTUALE,
                 silenzioso=True)

    stato["visti"] = sorted(visti)
    stato["aggiornato"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    salva_stato(stato)
    return cieco, totale


def main():
    una_volta = "--once" in sys.argv
    stato = carica_stato()

    if una_volta:
        cieco, totale = un_controllo(stato)
        log("cieco" if cieco else "%d slot utili prima del %s" % (totale, APPUNTAMENTO_ATTUALE))
        return

    log("avvio | soglia: %s | servizi: %s | ogni %ds"
        % (APPUNTAMENTO_ATTUALE, SERVIZI_INTERNAL_ID, INTERVALLO_SEC))
    telegram("Watcher BSN acceso.\nCerco slot prima del <b>%s</b>." % APPUNTAMENTO_ATTUALE,
             silenzioso=True)
    ultima_riga = None
    while True:
        inizio = time.time()
        cieco, totale = un_controllo(stato)
        riga = "cieco" if cieco else "%d slot utili" % totale
        if riga != ultima_riga:
            log(riga)
            ultima_riga = riga
        pausa = INTERVALLO_SEC - (time.time() - inizio) + random.uniform(0, 3)
        if pausa > 0:
            time.sleep(pausa)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("fermato a mano")
