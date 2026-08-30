#!/bin/bash
cd "$(dirname "$0")" || exit 1

if pgrep -f "bsn.py" > /dev/null; then
  echo "Il watcher e' gia' acceso. Chiudi pure questa finestra."
  sleep 4
  exit 0
fi

echo "Watcher BSN avviato. Lascia questa finestra aperta."
echo "Per fermarlo: Ctrl+C, oppure chiudi la finestra."
echo
exec /usr/bin/python3 bsn.py
