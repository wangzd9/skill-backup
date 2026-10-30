#!/bin/sh
exec /home/wang/.hermes/hermes-agent/venv/bin/python /home/wang/work/hermes-role-runtime/pool_entry.py morning --pool /home/wang/.hermes/role-pool.json --role COORDINATOR --hermes-home /home/wang/.hermes
