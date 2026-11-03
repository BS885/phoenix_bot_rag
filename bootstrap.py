"""Import this first in every entry point: makes Python trust the Windows cert store (NetFree)."""
import truststore

truststore.inject_into_ssl()
