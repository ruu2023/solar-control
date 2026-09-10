#!/bin/zsh
# Run from an iTerm2 window on the Mac mini (Screen Sharing), ONE time.
ID=$(id -u)
echo '==> stopping LaunchAgent + any serve during the grant'
launchctl bootout gui/$ID/com.solar-control.api 2>/dev/null || true
pkill -f 'serve_app.py|solar_control serve' 2>/dev/null || true
sleep 1

echo '==> launching SolarControl.app in the GUI session'
open ~/solar-control/SolarControl.app
echo
echo '   >>>>>>  A macOS dialog "“SolarControl” would like to use Bluetooth" should appear.  >>>>>>'
echo '   >>>>>>  Click  Allow  (or OK).                                                     >>>>>>'
echo
echo '   waiting up to ~100s for the grant + server...'
up=0
for i in $(seq 1 50); do
  sleep 2
  if curl -s --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then up=1; echo "   server is up (after ${i}x2s)"; break; fi
done
echo
echo '--- /health ---'; curl -s --max-time 10 http://127.0.0.1:8000/health; echo
echo '--- /status ---'; curl -s --max-time 30 http://127.0.0.1:8000/status; echo
echo
if [ "$up" != "1" ]; then
  echo '(server did not come up — grab the log and stop here)'
  tail -n 25 ~/Library/Logs/solar-control/solar-control.err.log
  echo; echo 'Paste this back to Claude.'
  exit 0
fi
printf 'If /status shows real numbers, press RETURN to hand off to the resident LaunchAgent (Ctrl-C to abort): '
read _
pkill -f 'serve_app.py|solar_control serve' 2>/dev/null || true
sleep 2
launchctl bootstrap gui/$ID ~/Library/LaunchAgents/com.solar-control.api.plist
sleep 14
echo '--- resident /health ---'; curl -s --max-time 10 http://127.0.0.1:8000/health; echo
echo '--- resident /status ---'; curl -s --max-time 30 http://127.0.0.1:8000/status; echo
echo '--- err tail ---'; tail -n 15 ~/Library/Logs/solar-control/solar-control.err.log
echo; echo 'Paste this whole output back to Claude.'
