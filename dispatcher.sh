export CLAUDE_CODE_SUBAGENT_MODEL="claude-opus-4-7"

while :; do
 git pull origin main
 claude \
  --channels plugin:telegram@claude-plugins-official \
  --permission-mode acceptEdits \
  --allowedTools 'mcp__plugin_telegram_telegram__*' \
  --name "🤖 Dispatcher" \
  /dispatcher
 echo ""
 echo "Relaunching in 5 seconds..."
 echo ""
 sleep 5
done
