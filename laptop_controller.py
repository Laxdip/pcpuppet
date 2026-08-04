"""
Lax
=============================================
Control your Windows laptop from your phone via a Flask web UI + ngrok.

"""

import os
import sys
import json
import time
import socket
import secrets
import logging
import platform
import threading
import subprocess
import webbrowser
from logging.handlers import RotatingFileHandler
from functools import wraps

