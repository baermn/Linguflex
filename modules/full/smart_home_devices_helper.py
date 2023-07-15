from core import log, DEBUG_LEVEL_MIN, DEBUG_LEVEL_MID, DEBUG_LEVEL_MAX, DEBUG_LEVEL_ERR

import time
import tinytuya
import re
import colorsys
import threading
import json
from hmac import new
from typing import Tuple, Dict

class SmartBulbThread(threading.Thread):
    def __init__(self, 
            bulb_param: Dict[str, str],
            connection_event: threading.Event(), 
            status_event: threading.Event(), 
            color_change_condition: threading.Condition()) -> None:
        super().__init__()
        self.color_change_condition = color_change_condition
        self.color = (0, 0, 0)
        self.bulb_param = bulb_param
        self.connection_event = connection_event
        self.status_event = status_event
        self.state = 'off'
        self.is_running = True

    def change_color(self, 
            color: Tuple[int, int, int]) -> None:
        with self.color_change_condition:
            self.color = color
            self.color_change_condition.notify()        

    def shutdown(self) -> None:
        self.is_running = False

    def set_color(self, 
            rgb_tupel: Tuple[int, int, int]) -> None:
        self.color = rgb_tupel
        log(DEBUG_LEVEL_MAX, f'  [lights] {self.bulb_param["name"]} color set to: {rgb_tupel}')        
        self.device.set_colour(rgb_tupel[0], rgb_tupel[1], rgb_tupel[2], nowait=True)        
        
        # we don't ever flood the bulb, so safest bet is forcewait after set color
        time.sleep(0.2) 

    def run(self) -> None:
        name = self.bulb_param["name"]
        id = self.bulb_param['id']
        ip = self.bulb_param['ip']
        key = self.bulb_param['key']
        deviceVersion = float(self.bulb_param['version'])
        # Connect to tuya bulb
        self.device = tinytuya.BulbDevice(id, ip, key)
        self.device.set_version(deviceVersion)
        log(DEBUG_LEVEL_MAX, f'  [lights] {name} connected')        
        self.connection_event.set()
        # Get status of bulb    
        data = self.device.status()
        if 'dps' in data:
            self.state = 'on'
            dps = data['dps']
            color = dps['24']
            self.color = self.hsv_string_to_rgb(color)
            log(DEBUG_LEVEL_MAX, f'  [lights] {name} color: {self.color}')
        self.status_event.set()
        # Enter worker cycle
        while self.is_running:
            with self.color_change_condition:
                notified = self.color_change_condition.wait(timeout=0.5)  
                if notified:
                    self.set_color(self.color)
                time.sleep(0.01)

    def hsv_string_to_rgb(self,
            hsv: str) -> Tuple[int, int, int]:
        color_value = int(hsv[0:4], 16)
        saturation_value = int(hsv[4:8], 16)
        brightness_value = int(hsv[8:12], 16) 
        # convert to values between 0 and 1
        color_value = color_value / 360.0
        saturation_value = saturation_value / 1000.0
        brightness_value = brightness_value / 1000.0
        # calculate rgb from hsv values
        hsv = (color_value, saturation_value, brightness_value)
        rgb = colorsys.hsv_to_rgb(*hsv)
        return (int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))

class LightManager():
    def __init__(self, bulbs) -> None:
        self.bulb_params = bulbs                        
        self.is_running = True
        self.is_rotation_active = False
        self.rotation_speed = 10
        self.rotation_update_pause = 0.5
        self.rotation_clockwise = True
        self.bulb_devices = []
        self.bulb_states = []
        self.bulb_colors = []
        self.bulb_rotation_colors = []
        self.bulbs_ready = False
        num_lamps = len(self.bulb_params)
        # Create event objects
        self.connection_events = [threading.Event() for _ in range(num_lamps)]
        self.status_events = [threading.Event() for _ in range(num_lamps)]
        color_change_conditions = [threading.Condition() for _ in range(num_lamps)]
        # Create and start lamp threads
        self.bulb_threads = [SmartBulbThread(self.bulb_params[i], self.connection_events[i], self.status_events[i], color_change_conditions[i]) for i in range(num_lamps)]
        for thread in self.bulb_threads:
            self.bulb_colors.append((0,0,0))
            thread.start()
        self.rotation_worker_thread = threading.Thread(target=self.rotation_worker)
        self.rotation_worker_thread.start()

    def shutdown(self) -> None:
        log(DEBUG_LEVEL_MAX, '  [lights] shutting down bulb threads')
        self.is_running = False
        for thread in self.bulb_threads:
            thread.shutdown()
        # Wait for all threads to finish
        for thread in self.bulb_threads:
            thread.join()

    def wait_ready(self) -> None:
        # Wait for all bulbs connected
        log(DEBUG_LEVEL_MAX, '  [lights] connecting to bulbs')
        for event in self.connection_events:
            event.wait()
        log(DEBUG_LEVEL_MID, '  [lights] bulbs connected')
        # Wait for all bulbs status requested
        for event in self.status_events:
            event.wait()
        log(DEBUG_LEVEL_MID, '  [lights] bulbs status check complete')
        # Init colors
        for bulb_thread in self.bulb_threads:
            i = self.find_bulb_index(bulb_thread.bulb_param['name'])
            self.bulb_colors[i] = bulb_thread.color
        self.bulbs_ready = True

    def set_colors_json_hex_string(self, 
            json: str) -> None:
        data = json.loads(json)
        self.set_colors_json_hex(data)

    def find_bulb_index(self, 
            name: str) -> int:
        for bulb in self.bulb_params:
            if bulb['name'] == name:
                index = self.bulb_params.index(bulb)
                return index
        return -1
    
    def is_valid_hex_color(self, s):
        """Check if a string is a valid RGB Hex color."""
        if re.match("^#[0-9a-fA-F]{6}$", s):
            return True
        else:
            return False
            
    def convert_from_hex(self, hex_color_str):
        """Convert a hexadecimal color string to a RGB tuple."""
        
        # remove the '#' at the beginning if present
        if hex_color_str.startswith('#'):
            hex_color_str = hex_color_str[1:]

        # convert hex to decimal
        red = int(hex_color_str[0:2], 16)
        green = int(hex_color_str[2:4], 16)
        blue = int(hex_color_str[4:6], 16)

        return (red, green, blue)        

    def set_color_hex(self, 
            bulb_name: str, 
            color_string: str) -> None:
        
        if not self.is_valid_hex_color(color_string):
            return {
                "result": "error",
                "reason" : "color must be hex string like #C0C0C0",
                "name" : bulb_name
            }            
        
        color = self.convert_from_hex(color_string)
        return self.set_color(bulb_name, color)
    
    def raise_color_information_event(self):
        self.get_colors()
        self.server.set_event("bulb_colors_changed", self.get_colors_json_rgb())

    def set_color(self, 
            bulb_name: str, 
            color: Tuple[int, int, int]) -> None:
        i = self.find_bulb_index(bulb_name)
        if i == -1:
            bulb_names = [b["Name"] for b in self.bulb_params]
            bulb_names_string = ', '.join(bulb_names)
            return {
                "result": "error",
                "reason" : f"bulb name does not exist, must be one of these: {bulb_names_string}",
                "name" : bulb_name
            }

        self.bulb_colors[i] = color
        for bulb_thread in self.bulb_threads:
            if bulb_thread.bulb_param['name'] == bulb_name:
                bulb_thread.change_color(color)
                return {
                    "result": "success",
                    "state" : color,
                    "name" : bulb_name
                }                

    def get_color_hex(self, 
            bulb_name: str) -> str:
        color = self.get_color(bulb_name)
        hex_color = '#{:02X}{:02X}{:02X}'.format(color)
        return hex_color
    
    def get_colors(self) -> None:
        for bulb_thread in self.bulb_threads:
            i = self.find_bulb_index(bulb_thread.bulb_param['name'])
            self.bulb_colors[i] = bulb_thread.color

    def get_color(self, 
            bulb_name: str) -> Tuple[int, int, int]:
        for bulb_thread in self.bulb_threads:
            if bulb_thread.bulb_param['name'] == bulb_name:
                i = self.find_bulb_index(bulb_name)
                self.bulb_colors[i] = bulb_thread.color
                return self.bulb_colors[i]

    def rotate(self, 
            speed = 10, 
            update_pause = 0.5, 
            clockwise = True):
        self.bulb_rotation_colors = []
        # Make a copy of the original bulb_colors list to perform rotation.
        for bulb in self.bulb_params:
            bulbname = bulb['name']
            color = self.get_color(bulbname)                    
            self.bulb_rotation_colors.append(color)
        self.rotation_speed = float(speed)
        self.rotation_update_pause = float(update_pause)
        self.rotation_clockwise = clockwise        
        self.is_rotation_active = True
    
    def rotation_worker(self) -> None:
        cycle = 0
        while self.is_running:
            if self.is_rotation_active:
                if cycle == 0: 
                    cycle = self.rotation_speed
                    original_colors = self.bulb_rotation_colors.copy()
                cycle_percent = (self.rotation_speed - cycle) / self.rotation_speed
                cycle -= 1
                # Calculate the new colors and update the bulbs.
                for i in range(len(self.bulb_params)):
                    # Determine the neighboring color index based on the rotation direction.
                    if self.rotation_clockwise:
                        next_idx = (i + 1) % len(self.bulb_params)
                    else:
                        next_idx = (i - 1) % len(self.bulb_params)
                    # Interpolate the color
                    new_color = self.interpolate_color(original_colors[i], original_colors[next_idx], cycle_percent)
                    bulbname = self.bulb_params[i]['name']
                    self.set_color(bulbname, new_color)                    
                if cycle == 0: 
                     if self.rotation_clockwise:
                        first_item = self.bulb_rotation_colors[0]
                        self.bulb_rotation_colors = self.bulb_rotation_colors[1:] + [first_item]
                     else:
                        last_item = self.bulb_rotation_colors[-1]
                        self.bulb_rotation_colors = [last_item] + self.bulb_rotation_colors[:-1]
            time.sleep(self.rotation_update_pause)

    def get_names(self) -> str:
        return [entry['name'] for entry in self.bulb_params]

    def stop(self) -> None:
        self.is_rotation_active = False

    def get_colors_json_rgb_dump(self):
        compressed_data = {}
        for i, color in enumerate(self.bulb_colors):
            name = self.bulb_params[i]['name']
            compressed_data[name] = color
        return json.dumps(self.get_colors_json_rgb(), separators=(',', ':'))    
    
    def get_colors_json_rgb(self):
        compressed_data = {}
        for i, color in enumerate(self.bulb_colors):
            name = self.bulb_params[i]['name']
            compressed_data[name] = color
        return compressed_data
    
    def get_colors_json_hex_dump(self):
        return json.dumps(self.get_colors_json_hex(), separators=(',', ':'))    
    
    def get_colors_json_hex(self):
        compressed_data = {}
        for i, color in enumerate(self.bulb_colors):
            name = self.bulb_params[i]['name']
            hex_color = '#{:02X}{:02X}{:02X}'.format(*color)
            compressed_data[name] = hex_color
        return compressed_data

    def interpolate_color(self, 
            color1: Tuple[int, int, int], 
            color2: Tuple[int, int, int], 
            interpolationGrade: float) -> Tuple[int, int, int]:
        rDist = color2[0] - color1[0]
        gDist = color2[1] - color1[1]
        bDist = color2[2] - color1[2]
        r = int(color1[0] + interpolationGrade * rDist)
        g = int(color1[1] + interpolationGrade * gDist)
        b = int(color1[2] + interpolationGrade * bDist)
        return (r, g, b)            
    
    # convert hhhhssssvvvv hsv color string to (R, G, B)
    def hsv_string_to_rgb(self, 
            hsv: str) -> Tuple[int, int, int]:
        color_value = int(hsv[0:4], 16)
        saturation_value = int(hsv[4:8], 16)
        brightness_value = int(hsv[8:12], 16)
        # convert to values between 0 and 1
        color_value = color_value / 360.0
        saturation_value = saturation_value / 1000.0
        brightness_value = brightness_value / 1000.0
        # calculate rgb from hsv values
        hsv = (color_value, saturation_value, brightness_value)
        rgb = colorsys.hsv_to_rgb(*hsv)
        return (int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
    

class OutletDeviceThread(threading.Thread):
    def __init__(self, 
            outlet_param: Dict[str, str],
            connection_event: threading.Event(), 
            status_event: threading.Event(), 
            state_change_condition: threading.Condition()) -> None:
        super().__init__()
        self.state_change_condition = state_change_condition
        self.outlet_param = outlet_param
        self.connection_event = connection_event
        self.status_event = status_event
        self.state = False
        self.is_running = True

    def shutdown(self) -> None:
        self.is_running = False

    def change_state(self, 
            state: bool) -> None:
        with self.state_change_condition:
            self.state = state
            self.state_change_condition.notify()        

    def set_state(self, 
            state: bool) -> None:
        self.state = state
        log(DEBUG_LEVEL_MAX, f'  [outlets] {self.outlet_param["name"]} state set to: {state}')        
        if state:
            self.device.turn_on(nowait=True)
        else:
            self.device.turn_off(nowait=True)
        # we don't ever dds the plug, so safest bet is forcewait after set state
        time.sleep(0.2) 

    def run(self) -> None:
        name = self.outlet_param["name"]
        id = self.outlet_param['id']
        ip = self.outlet_param['ip']
        key = self.outlet_param['key']
        deviceVersion = float(self.outlet_param['version'])
        # Connect to outlet
        self.device = tinytuya.OutletDevice(id, ip, key)
        self.device.set_version(deviceVersion)
        log(DEBUG_LEVEL_MAX, f'  [outlets] {name} connected')        
        self.connection_event.set()
        # Get status of outlet    
        data = self.device.status()
        # print (f"Status {data}")
        self.state = False
        if 'dps' in data:
            dps = data['dps']
            # print (f"Data {dps}")
            self.state = dps['1']
            # print (f"STATE {state}")
        else: 
            log(DEBUG_LEVEL_MIN, f'  [outlets] NO DATE for {name}')
        log(DEBUG_LEVEL_MAX, f'  [outlets] {name} state: {self.state}')
        self.status_event.set()
        # Enter worker cycle
        while self.is_running:
            with self.state_change_condition:
                notified = self.state_change_condition.wait(timeout=0.5)  
                if notified:
                    self.set_state(self.state)
                time.sleep(0.01)

class OutletManager():
    def __init__(self, outlets) -> None:
        self.outlet_param = outlets
        self.is_running = True

        num_outlets = len(self.outlet_param)
        self.connection_events = [threading.Event() for _ in range(num_outlets)]
        self.status_events = [threading.Event() for _ in range(num_outlets)]
        state_change_conditions = [threading.Condition() for _ in range(num_outlets)]
        self.outlet_threads = [OutletDeviceThread(self.outlet_param[i], self.connection_events[i], self.status_events[i], state_change_conditions[i]) for i in range(num_outlets)]
        for thread in self.outlet_threads:
            thread.start()   

    def set_state(self, 
            outlet_name: str, 
            state: bool) -> None:
        i = self.find_outlet_index(outlet_name)
        if i == -1:
            return {
                "result": "error",
                "reason" : "outlet name not found",
                "name" : outlet_name
            }
        for outlet_thread in self.outlet_threads:
            if outlet_thread.outlet_param['name'].lower() == outlet_name.lower():
                outlet_thread.change_state(state)
                return {
                    "result": "success",
                    "state" : state,
                    "name" : outlet_name
                }

    def find_outlet_index(self, 
            name: str) -> int:
        for outlet in self.outlet_param:
            if outlet['name'].lower() == name.lower():
                return self.outlet_param.index(outlet)
        return -1

    def get_state(self, 
            outlet_name: str) -> bool:
        for outlet_thread in self.outlet_threads:
            if outlet_thread.outlet_param['name'].lower() == outlet_name.lower():
                i = self.find_outlet_index(outlet_name)
                if i == -1:
                    return {
                        "result": "error",
                        "reason" : "outlet name not found",
                        "name" : outlet_name
                    }
                return {
                    "result": "success",
                    "state" : outlet_thread.state,
                    "name" : outlet_name
                }

    def shutdown(self) -> None:
        log(DEBUG_LEVEL_MAX, '  [outlets] shutting down smart plug threads')
        self.is_running = False
        for thread in self.outlet_threads:
            thread.shutdown()
        # Wait for all threads to finish
        for thread in self.outlet_threads:
            thread.join()