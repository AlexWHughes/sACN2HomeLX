# sACN2LIFX

Control LIFX lights via sACN/E1.31 with automatic discovery and web-based mapping interface.

![sACN2LIFX Interface](templates/Example.png)

## Features

- **Automatic LIFX Discovery**: Automatically discovers all LIFX lights on your network using the LIFX LAN protocol
- **Web UI**: Clean, intuitive web interface for mapping lights to DMX universes and channels
- **E1.31/sACN Support**: Receives DMX data via sACN (E1.31) protocol with real-time status monitoring
- **Test RGB Mode**: Test lights directly without DMX input - useful for debugging and verification
- **Network Interface Selection**: Choose specific network interfaces for LIFX discovery and sACN reception
- **Manual Light Addition**: Add lights by IP address if they're not discoverable automatically
- **Persistent Mappings**: Automatically saves your light mappings and settings to `config.json`
- **Per-Light Brightness Control**: Adjust brightness multiplier (0-100%) for each light individually
- **Real-Time Status**: Live DMX reception status showing active universes and packet counts
- **Channel Mode Support**: Multiple channel modes for flexible DMX control:
  - **RGB (8bit)**: 3 channels - Red, Green, Blue (0-255 each)
  - **RGB (16bit)**: 6 channels - Red, Green, Blue with 16-bit precision (MSB+LSB per color, 0-65535)
  - **RGBW (8bit)**: 4 channels - Red, Green, Blue, White
  - **RGBW (16bit)**: 8 channels - Red, Green, Blue, White with 16-bit precision (MSB+LSB per color, 0-65535)
  - **HSBK (8bit)**: 4 channels - Hue, Saturation, Brightness, Kelvin (2500-9000K)
  - **HSBK (16bit)**: 8 channels - HSBK with 16-bit precision (MSB+LSB per parameter)
- **Optimized Performance**: 
  - 20ms fade duration for smooth 40Hz sACN transitions
  - 50Hz LIFX command rate limit for responsive updates
  - Value change threshold to minimize unnecessary updates
- **Configuration Reload**: Reload configuration from disk without restarting the application
- **Thread-Safe**: Robust multi-threaded architecture for reliable DMX processing

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

1. Start the server:
```bash
python app.py
```

2. Open your browser to `http://localhost:5001`

3. **Configure Network Interfaces** (if needed):
   - Select the network interface for LIFX light discovery
   - Select the network interface for sACN (DMX) reception
   - Click "Save & Apply Settings"

4. **Discover Lights**:
   - Click "Discover Lights" to find all LIFX devices on your network
   - Lights will appear in the "Discovered Lights" section

5. **Configure Light Mappings**:
   For each light, configure:
   - **Universe**: The DMX universe number (typically 1-512)
   - **Start Channel**: The first DMX channel for this light
   - **Brightness**: Overall brightness multiplier (0-100%)
   - **Channel Mode**: Select from available modes (RGB (8bit): 3 channels, RGB (16bit): 6 channels, RGBW (8bit): 4 channels, RGBW (16bit): 8 channels, HSBK (8bit): 4 channels, HSBK (16bit): 8 channels)

6. **Test Lights** (Optional):
   - Use the "Test RGB (DMX-less)" section to test lights directly
   - Enter RGB values (0-255) or use quick color buttons
   - Adjust brightness and click "Test" to send colour to the light

7. **Start DMX Processing**:
   - Click "Start DMX" to begin processing sACN data
   - Monitor the status bar for DMX reception status
   - Active universes and packet counts are displayed in real-time

8. **Stop DMX Processing**:
   - Click "Stop DMX" to halt DMX processing while keeping the server running

## DMX Channel Mapping

### RGB (8bit) - 3 channels
- Channel N: Red (0-255)
- Channel N+1: Green (0-255)
- Channel N+2: Blue (0-255)

### RGB (16bit) - 6 channels
- Channel N: Red MSB (Most Significant Byte)
- Channel N+1: Red LSB (Least Significant Byte) → Combined: 0-65535
- Channel N+2: Green MSB
- Channel N+3: Green LSB → Combined: 0-65535
- Channel N+4: Blue MSB
- Channel N+5: Blue LSB → Combined: 0-65535

### RGBW (8bit) - 4 channels
- Channel N: Red (0-255)
- Channel N+1: Green (0-255)
- Channel N+2: Blue (0-255)
- Channel N+3: White (0-255)

### RGBW (16bit) - 8 channels
- Channel N: Red MSB
- Channel N+1: Red LSB → Combined: 0-65535
- Channel N+2: Green MSB
- Channel N+3: Green LSB → Combined: 0-65535
- Channel N+4: Blue MSB
- Channel N+5: Blue LSB → Combined: 0-65535
- Channel N+6: White MSB
- Channel N+7: White LSB → Combined: 0-65535

### HSBK (8bit) - 4 channels
- Channel N: Hue (0-255 → 0-360°)
- Channel N+1: Saturation (0-255 → 0-100%)
- Channel N+2: Brightness (0-255 → 0-100%)
- Channel N+3: Kelvin (0-255 → 2500-9000K)

### HSBK (16bit) - 8 channels
- Channel N: Hue MSB
- Channel N+1: Hue LSB → Combined: 0-65535 → 0-360°
- Channel N+2: Saturation MSB
- Channel N+3: Saturation LSB → Combined: 0-65535 → 0-100%
- Channel N+4: Brightness MSB
- Channel N+5: Brightness LSB → Combined: 0-65535 → 0-100%
- Channel N+6: Kelvin MSB
- Channel N+7: Kelvin LSB → Combined: 0-65535 → 2500-9000K

**Example**: If a light is mapped to Universe 1, Channel 1 in RGB (8bit) mode:
- Channel 1 = Red
- Channel 2 = Green
- Channel 3 = Blue

## Test RGB Mode

The "Test RGB (DMX-less)" feature allows you to:
- Test lights without requiring sACN/DMX input
- Debug refresh and smoothness issues
- Verify light connectivity and colour accuracy
- Quickly test different RGB values and brightness levels

Simply enter RGB values (0-255), adjust brightness (0-100%), and click "Test" to send the colour directly to the configured light.

## Configuration

Mappings and settings are automatically saved to `config.json` in the project directory. The configuration includes:
- Light mappings (universe, start channel, brightness, channel mode)
- Network interface settings (LIFX and sACN interfaces)
- Light labels and IP addresses

You can reload the configuration without restarting the application using the "Reload Config" button.

## Performance Tuning

The application is optimized for smooth 40Hz sACN input:
- **Fade Duration**: 20ms (configurable via `FADE_DURATION_MS`)
- **LIFX Rate Limit**: 50Hz (20ms minimum interval between commands)
- **Value Change Threshold**: 1 DMX value (only updates if change exceeds threshold)

These settings can be adjusted in `app.py` if needed for different sACN frame rates.

## Requirements

- Python 3.7+
- LIFX lights on the same network
- DMX/E1.31 source (e.g., lighting console, software)
- Network interface with multicast support for sACN

## Technical Details

- **Protocol**: sACN (E1.31) for DMX reception, LIFX LAN Protocol for light control
- **Threading**: Multi-threaded architecture with proper synchronization for DMX processing
- **State Management**: Thread-safe state updates with protection against race conditions
- **Error Handling**: Robust error handling with graceful degradation

## Troubleshooting

- **Lights not discovered**: Ensure lights are on the same network and powered on. Try using "Manually Add Light" with the light's IP address.
- **DMX not receiving**: Check that the sACN interface is correctly configured and that your DMX source is sending to the correct universe.
- **Stepping/jerky transitions**: The application is optimized for 40Hz sACN. If using a different frame rate, you may need to adjust `FADE_DURATION_MS` and `VALUE_CHANGE_THRESHOLD` in `app.py`.
- **Configuration not saving**: Ensure the application has write permissions in the project directory.

## Credits

Special thanks to [@lyndonbuckley](https://github.com/lyndonbuckley) for suggesting the addition of 16-bit RGB and HSBK control modes, which provide higher precision color control for professional lighting applications.
