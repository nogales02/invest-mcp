"""Calibration of InVEST hydrological models (AWY / SWY / SDR / NDR).

The numerical engine is the shared ``invest_calibration_assistant.core`` package
(the same core the InVEST Workbench "Calibration Assistant" plugin uses). It runs
in the ``invest-cal`` conda env (natcap.invest + spotpy); this MCP server only
builds the config, launches the worker as a subprocess, streams progress and
parses the result.
"""
