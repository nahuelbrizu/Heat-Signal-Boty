"""Alertas de oportunidad (fase 3 del plan "estrategias generadas + alertas", 2026-11-21).

`store.py` (sin pandas: lo usa el panel) guarda alertas, corridas, acciones e interruptores de automatico en
`data/alerts.sqlite` (append-only). `engine.py` (pandas, proceso aparte cada 5 min) corre las reglas del catalogo y
las generadas sobre las velas 1m cerradas y registra cada entrada / salida / giro nuevo. Ver `__main__.py`.
"""
