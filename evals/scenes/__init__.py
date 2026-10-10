"""The hand-written scenes the golden set is built from, one module per tool family."""

from evals.scenes import database, email, filesystem, git, http

FAMILIES = {
    "filesystem": filesystem,
    "git": git,
    "http": http,
    "database": database,
    "email": email,
}
