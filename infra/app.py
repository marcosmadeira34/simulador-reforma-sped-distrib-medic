#!/usr/bin/env python3
import aws_cdk as cdk

from stack import SimuladorRtcStack

app = cdk.App()
SimuladorRtcStack(app, "SimuladorRtc")
cdk.Tags.of(app).add("projeto", "simulador-rtc")
app.synth()
