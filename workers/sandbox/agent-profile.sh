# shellcheck shell=sh
# ~/.profile of `agent` (login shells, e.g. Hermes' `bash -l` snapshot). Replaces Debian's skel copy, which
# PREPENDS ~/bin and ~/.local/bin to PATH and so lets anything installed there shadow aios-check, git or
# python3. PATH comes from sshd's SetEnv and /etc/profile.d/aios.sh (user dirs appended).
if [ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ]; then
  # shellcheck source=/dev/null
  . "$HOME/.bashrc"
fi
