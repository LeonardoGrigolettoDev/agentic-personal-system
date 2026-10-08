# shellcheck shell=sh
# /etc/profile.d/aios.sh: login shells (Hermes snapshots the environment with `bash -l`) get PATH reset by
# /etc/profile; append the user-level tool dirs that sshd's SetEnv provides to non-login sessions. Appended,
# never prepended: what the agent installs there must not shadow aios-check, git, ssh or python3.
case ":$PATH:" in
  *":$HOME/go/bin:"*) ;;
  *) PATH="$PATH:/usr/local/go/bin:$HOME/go/bin:$HOME/.local/bin" ;;
esac
export PATH
