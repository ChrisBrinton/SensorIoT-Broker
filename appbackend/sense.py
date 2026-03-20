from sense_energy import Senseable


username = "keyvanazami@gmail.com"
password = "!"
sense = Senseable()
try:
	sense.authenticate(username, password)
	sense.update_realtime()
except err:
	print(err)

#sense.update_trend_data()
print ("Active:", sense.active_power, "W")
print ("Active Solar:", sense.active_solar_power, "W")
print ("Daily:", sense.daily_usage, "KWh")
print ("Daily Solar:", sense.daily_production, "KWh")
print ("Active Devices:",", ".join(sense.active_devices))

