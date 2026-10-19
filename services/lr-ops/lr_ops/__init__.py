"""Local LR-Ops service: the Python side of the Lightroom bridge.

The Lightroom plugin polls this service on 127.0.0.1 for jobs and posts
results back. The service never initiates connections into Lightroom.
"""

__version__ = "0.6.2"
