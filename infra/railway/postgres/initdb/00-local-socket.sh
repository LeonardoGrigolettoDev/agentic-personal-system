# shellcheck shell=bash
# Sourced (not executed: the image installs it 0644) by the postgres entrypoint before 01-init.sh. Executable
# initdb scripts inherit the container environment, and a bare `psql` follows PGHOST/PGHOSTADDR over TCP while
# the first-boot server listens on the Unix socket only (listen_addresses=''): the init would abort and the next
# boot would skip it, leaving a cluster without roles or databases. Unsetting them here pins the socket.
unset PGHOST PGHOSTADDR
