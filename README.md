# Parkplatz-Wächter

Sucht alle 10 Minuten neue Mietparkplätze in Gehdistanz und schickt einen Push aufs iPhone (ntfy).

- **Quellen:** Flatfox (Schnittstelle mit genauem Standort), tutti.ch. Homegate, ImmoScout24, Newhome und Comparis sperren automatische Abfragen. Für diese Portale ein Suchabo einrichten.
- **Filter** (`config.json`): Wunsch 7 Min., Maximum 10 Min. zu Fuss (echte Fusswege über OpenStreetMap), bis CHF 250/Mt., nur Miete, keine Velo- und Motorradplätze.
- **Push:** Tiefgarage, Einzelgarage und Carport sofort und mit hoher Priorität, auch nachts. Aussenplätze kommen nachts (22–7 Uhr) gesammelt um 7 Uhr. Plätze mit 8–10 Min. Gehzeit sind als „etwas weiter“ markiert.
- **Sammel-Meldungen** (erster Lauf, mehr als 8 neue Plätze, Aussenplätze aus der Nacht) öffnen `docs/liste.html`. Dort ist jedes Inserat einzeln antippbar.
- **Antippen** öffnet das Inserat. Der Knopf „Anfragetext“ öffnet `docs/anfrage.html` mit einem fertigen Text in der Sie-Form.

## Einrichten

1. **iPhone:** App **ntfy** aus dem App Store laden, auf **+** tippen und den Kanal aus `NTFY_TOPIC` abonnieren (Server: ntfy.sh). Mitteilungen erlauben. Für die Nacht ntfy im Fokus „Schlafen“ als erlaubte App eintragen.
2. **GitHub:** Ein öffentliches Repository mit diesen Dateien anlegen. Bei öffentlichen Repositories sind die Actions-Minuten gratis. Bei privaten reichen die Gratis-Minuten für einen Lauf alle 10 Minuten nicht.
3. Unter **Settings → Secrets and variables → Actions**:
   - Secrets: `NTFY_TOPIC`, `HOME_LAT`, `HOME_LON` (die Adresse bleibt so geheim)
   - Variable (optional): `ANFRAGE_SEITE` = `https://<benutzer>.github.io/<repo>/anfrage.html`
4. **Settings → Pages:** Branch `main`, Ordner `/docs`, damit die Anfragetext-Seite erreichbar ist.
5. Unter **Actions → Parkplatz-Wächter → Run workflow** einmal von Hand starten. Der erste Lauf schickt eine Übersicht aller Plätze, die schon jetzt passen. Danach kommt nur noch ein Push, wenn ein neues Inserat erscheint.

## Mail-Brücke (Homegate, ImmoScout24, Newhome, Comparis, tutti)

Diese Portale lassen sich nicht direkt abfragen. Der Wächter liest stattdessen ihre Suchabo-Mails aus einem eigenen Postfach-Ordner. Er liest nur und verändert keine Mails.

1. Auf jedem Portal ein Suchabo mit E-Mail anlegen: Parkplatz/Garage mieten, PLZ 8003, 8004 und 8005 (oder Umkreis), bis CHF 250, Benachrichtigung „sofort“ bzw. „täglich“, wenn es nichts anderes gibt.
2. Im Postfach einen Filter anlegen, der Mails dieser Absender in den Ordner bzw. das Label `Parkplatz-Alarm` legt.
3. Für das Postfach ein App-Passwort erstellen (Gmail: Bestätigung in zwei Schritten einschalten, dann myaccount.google.com/apppasswords).
4. Auf GitHub unter **Settings → Secrets and variables → Actions** die Secrets `MAIL_USER` (Mail-Adresse) und `MAIL_APP_PASSWORD` (App-Passwort) anlegen. Bei einem anderen Anbieter als Gmail zusätzlich die Variable `MAIL_IMAP_HOST` setzen, bei einem anderen Ordnernamen `MAIL_ORDNER`.

Eine gespeicherte Suchabo-Mail (.eml) lässt sich lokal prüfen: `python3 waechter.py --mail-datei mail.eml`.

## Lokal testen

```bash
set -a; source .env.local; set +a
python3 waechter.py --dry-run     # zeigt die Treffer, sendet nichts
python3 waechter.py --test-push   # schickt einen Beispiel-Push
python3 waechter.py --alle        # schickt alle aktuell passenden Plätze als Liste
```

Hinweis: GitHub startet geplante Läufe manchmal mit einigen Minuten Verspätung.
