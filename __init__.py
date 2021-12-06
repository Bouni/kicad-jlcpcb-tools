# import logging
# import os

from .plugin import JLCPCBPlugin

# logging.basicConfig(
#     level=logging.DEBUG,
#     format="%(asctime)s [%(levelname)s] %(message)s",
#     handlers=[
#         logging.FileHandler(
#             os.path.join(os.path.dirname(os.path.realpath(__file__)), "debug.log")
#         ),
#         logging.StreamHandler(),
#     ],
# )

# LOGGER = logging.getLogger()

# try:
JLCPCBPlugin().register()
# except Exception as e:
# LOGGER.debug(repr(e))
