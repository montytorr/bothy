"""Bothy's tests.

They assert PROPERTIES, not implementation. Every one of these was demonstrated
by hand at least once — against real Codex, real GitHub, a real systemd install
— and then lost, because the script that proved it was never saved. This is that
evidence, written down where the next change has to keep passing it.

Standard library `unittest` only, like everything else here. Run them with:

    python3 -m unittest discover -s tests -v

Nothing in here touches the network, spends money, or writes outside a
temporary directory.
"""
