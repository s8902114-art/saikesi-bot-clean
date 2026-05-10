#!/bin/bash
git add main.py backtest.py requirements.txt Procfile runtime.txt
git commit -m "更新：$(date '+%Y-%m-%d %H:%M')"
git push origin main
