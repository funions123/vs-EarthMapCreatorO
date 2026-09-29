uniform vec4 colorMapRects[40];
uniform float seasonRel;
uniform float seaLevel;
uniform float atlasHeight;
uniform float seasonTemperature;

out vec2 climateColorMapUv;
out vec2 seasonColorMapUv;

out float seasonWeight;
out float heretemp;
out float frostAlpha;

void calcColorMapUvs(int colormapData, vec4 worldPos, float sunlightLevel, bool isLeaves) {
	int seasonMapIndex = (colormapData & 0x3f) - 1;
	int climateMapIndex = ((colormapData >> 8) & 0xf) - 1;
	int frostableBit = (colormapData >> 12) & 1;
	float tempRel = clamp(((colormapData >> 16) & 0xff) / 255.0, 0.001, 0.999);
	float rainfallRel = clamp(((colormapData >> 24) & 0xff) / 255.0, 0.001, 0.999);

	frostAlpha=0;
	heretemp = tempRel + seasonTemperature;
	if (frostableBit > 0 && heretemp < 0.333) {
		frostAlpha = (valuenoise(worldPos.xyz / 2) + valuenoise(worldPos.xyz * 2)) * 1.25 - 0.5;
		frostAlpha -= max(0.0, 1 - pow(2*sunlightLevel, 10));
	}

	if (climateMapIndex >= 0) {
		vec4 rect = colorMapRects[climateMapIndex];
		climateColorMapUv = vec2(
			rect.x + rect.z * tempRel,
			rect.y + rect.w * rainfallRel
		);
	} else {
		climateColorMapUv = vec2(-1,-1);
	}

	if (seasonMapIndex >= 0) {
		vec4 rect = colorMapRects[seasonMapIndex];

		float noise;
		float b = valuenoise(worldPos.xyz) + valuenoise(worldPos.xyz/2);

		if (isLeaves) {
			int perTreeOffset = (colormapData >> 13) & 7;
			if (perTreeOffset != 0) {
				b += (perTreeOffset / 7.0 - 0.5) * 2.5;
			}

			noise = (valuenoise(worldPos.xyz / 6) + valuenoise(worldPos.xyz / 2) - 0.55) * 1.25;
		} else {
			noise = (valuenoise(worldPos.xyz / 24) + valuenoise(worldPos.xyz / 12) - 0.55) * 1.25;
		}

		seasonColorMapUv = vec2(
			rect.x + rect.z * clamp(seasonRel + b/40, 0.01, 0.99),
			rect.y + rect.w * clamp(noise, 0.5 / (rect.w * atlasHeight), 15.5 / (rect.w * atlasHeight))
		);

		if ((colormapData & 0xc0) == 0x40)
		{
			seasonWeight = clamp((tempRel + seasonTemperature / 2) * 0.9 - 0.1, 0.0, 1.0) * clamp(2 * (0.5 - cos(rainfallRel * 255.0 / 42.0)) / 2.1, 0.1, 0.75);
		} else {
			float x = tempRel * 255;
			// Vanilla reverses its sea-level lapse here. Bit 7 marks an Earth climate
			// temperature already measured at local terrain; do not add altitude.
			if ((colormapData & 0x80) == 0) {
				float seaLevelDist = worldPos.y - seaLevel;
				x += max(0.0, seaLevelDist * 1.5);
			}

			seasonWeight = clamp(0.5 - cos(x / 42.0) / 2.3 + max(0.0, 128 - x) / 256 / 2 - max(0.0,x - 130)/200, 0.0, 1.0);
		}

	} else {
		seasonColorMapUv = vec2(-1,-1);
	}
}
