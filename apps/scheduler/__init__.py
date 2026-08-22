"""Singleton control-plane process: cron materialisation and crash recovery.

Separated from the worker because these are *fleet-wide* responsibilities that
must happen exactly once, not once per worker. Running the reaper inside every
worker would mean ten processes racing to recover the same job; running cron
inside every worker would fire every schedule ten times.
"""
