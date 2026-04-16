export CLAUDE_CODE_SUBAGENT_MODEL="claude-opus-4-7"

# Default permission mode; suppressed if the caller overrides it via "$@".
perm_args=(--permission-mode acceptEdits)
for arg in "$@"; do
 case "$arg" in
  --permission-mode|--permission-mode=*|--dangerously-skip-permissions)
   perm_args=()
   break
   ;;
 esac
done

while :; do
 git pull origin main
 claude \
  --channels plugin:telegram@claude-plugins-official \
  "${perm_args[@]}" \
  --allowedTools 'mcp__plugin_telegram_telegram__*' \
  --name "🤖 Dispatcher" \
  "$@" \
  /dispatcher
 echo ""
 echo "Relaunching in 5 seconds..."
 echo ""
 sleep 5
done
