from yolo_uno import *
from pins import *
from lcd1602 import *
from dht20 import *
from rgb_led import *


import asyncio


def hex_to_rgb(color):
  color = color.replace('#', '')
  return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))


class Semaphore:
  def __init__(self, value=1):
    if value < 0:
      raise ValueError('ValueError')
    self.value = value
    self.waiting = []

  async def acquire(self):
    token = object()
    self.waiting.append(token)

    while self.value <= 0 or self.waiting[0] is not token:
      await asleep_ms(10)

    self.waiting.pop(0)
    self.value -= 1
    return True

  def release(self):
    self.value += 1


class RTOSQueue:
  def __init__(self, max_items=5):
    self.queue = []
    self.max_items = max_items
    self.item_ready = Semaphore(0)
    self.mutex = Semaphore(1)

  async def put(self, item):
    await self.mutex.acquire()
    try:
      if len(self.queue) >= self.max_items:
        self.queue.pop(0)
      else:
        self.item_ready.release()
      self.queue.append(item)
    finally:
      self.mutex.release()

  async def get(self):
    await self.item_ready.acquire()
    await self.mutex.acquire()
    try:
      return self.queue.pop(0)
    finally:
      self.mutex.release()


class ClimateConfig:
  def __init__(self):
    self.safe_temp_min = 22.0
    self.safe_temp_max = 28.0
    self.danger_temp_min = 18.0
    self.danger_temp_max = 32.0
    self.cooler_temp_on = 30.0
    self.humidity_min = 40.0

    self.color_green = '#00ff00'
    self.color_orange = '#ff9000'
    self.color_red = '#ff0000'
    self.color_yellow = '#ffff00'
    self.color_off = '#000000'


class ClimatePacket:
  def __init__(self, sequence, temperature, humidity, heater_state, cooler_on, humidifier_on):
    self.sequence = sequence
    self.temperature = temperature
    self.humidity = humidity
    self.heater_state = heater_state
    self.cooler_on = cooler_on
    self.humidifier_on = humidifier_on


def get_heater_state(temperature, config):
  if config.safe_temp_min <= temperature <= config.safe_temp_max:
    return 'SAFE'
  if config.danger_temp_min <= temperature <= config.danger_temp_max:
    return 'WARNING'
  return 'CRITICAL'


async def add_packet_to_all_queues(packet, queue_list):
  for task_queue in queue_list:
    await task_queue.put(packet)


async def safe_print(io_mutex, message):
  await io_mutex.acquire()
  try:
    print(message)
  finally:
    io_mutex.release()


async def safe_lcd_show(io_mutex, lcd1602, packet):
  cooler_text = 'ON' if packet.cooler_on else 'OK'
  humidifier_text = 'ON' if packet.humidifier_on else 'OK'

  await io_mutex.acquire()
  try:
    lcd1602.clear()
    lcd1602.show('T:{:4.1f}C H:{:4.1f}%'.format(packet.temperature, packet.humidity), 0, 0)
    lcd1602.show('Ht:{} Cl:{} Hm:{}'.format(packet.heater_state[0], cooler_text, humidifier_text), 1, 0)
  finally:
    io_mutex.release()


async def safe_rgb_show(actuator_mutex, rgb_led, color):
  await actuator_mutex.acquire()
  try:
    rgb_led.show(0, hex_to_rgb(color))
  finally:
    actuator_mutex.release()


async def task_LED_Blinky(led_D13):
  while True:
    led_D13.toggle()
    await asleep_ms(1000)


async def task_Read_Temperature(dht20, queue_list, config):
  sequence = 0

  while True:
    sequence += 1
    temperature = await dht20.atemperature()
    humidity = await dht20.ahumidity()

    packet = ClimatePacket(
      sequence,
      temperature,
      humidity,
      get_heater_state(temperature, config),
      temperature > config.cooler_temp_on,
      humidity < config.humidity_min
    )

    await add_packet_to_all_queues(packet, queue_list)
    await asleep_ms(5000)


async def task_LCD_Display(display_queue, lcd1602, io_mutex):
  while True:
    packet = await display_queue.get()
    cooler_text = 'ON' if packet.cooler_on else 'OK'
    humidifier_text = 'ON' if packet.humidifier_on else 'OK'

    await safe_print(
      io_mutex,
      'Sample {}: temp={:.1f}C humidity={:.1f}% heater={} cooler={} humidifier={}'.format(
        packet.sequence,
        packet.temperature,
        packet.humidity,
        packet.heater_state,
        cooler_text,
        humidifier_text
      )
    )
    await safe_lcd_show(io_mutex, lcd1602, packet)


async def task_Heater(heater_queue, rgb_led_D3, actuator_mutex, io_mutex, config):
  while True:
    packet = await heater_queue.get()

    if packet.heater_state == 'SAFE':
      await safe_rgb_show(actuator_mutex, rgb_led_D3, config.color_green)
    elif packet.heater_state == 'WARNING':
      await safe_rgb_show(actuator_mutex, rgb_led_D3, config.color_orange)
    else:
      await safe_rgb_show(actuator_mutex, rgb_led_D3, config.color_red)

    await safe_print(io_mutex, 'Heater Task: {} at {:.1f}C'.format(packet.heater_state, packet.temperature))


async def task_Cooler(cooler_queue, rgb_led_D5, actuator_mutex, io_mutex, config):
  while True:
    packet = await cooler_queue.get()

    if packet.cooler_on:
      await safe_print(io_mutex, 'Cooler Task: ON for 5s at {:.1f}C'.format(packet.temperature))
      await safe_rgb_show(actuator_mutex, rgb_led_D5, config.color_green)
      await asleep_ms(5000)
      await safe_rgb_show(actuator_mutex, rgb_led_D5, config.color_off)
      await safe_print(io_mutex, 'Cooler Task: recheck after fixed duration')
    else:
      await safe_rgb_show(actuator_mutex, rgb_led_D5, config.color_off)
      await safe_print(io_mutex, 'Cooler Task: OFF at {:.1f}C'.format(packet.temperature))


async def task_Humidifier(humidifier_queue, rgb_led_D7, actuator_mutex, io_mutex, config):
  while True:
    packet = await humidifier_queue.get()

    if packet.humidifier_on:
      await safe_print(io_mutex, 'Humidifier Task: start sequence at {:.1f}%'.format(packet.humidity))
      await safe_rgb_show(actuator_mutex, rgb_led_D7, config.color_green)
      await asleep_ms(5000)
      await safe_rgb_show(actuator_mutex, rgb_led_D7, config.color_yellow)
      await asleep_ms(3000)
      await safe_rgb_show(actuator_mutex, rgb_led_D7, config.color_red)
      await asleep_ms(2000)
      await safe_rgb_show(actuator_mutex, rgb_led_D7, config.color_off)
      await safe_print(io_mutex, 'Humidifier Task: sequence done, wait next queue packet')
    else:
      await safe_rgb_show(actuator_mutex, rgb_led_D7, config.color_off)
      await safe_print(io_mutex, 'Humidifier Task: OFF at {:.1f}%'.format(packet.humidity))


async def setup():
  print('Tiny RTOS Smart Climate Controller started')
  print('Queue design: ClimatePacket object is copied into task queues')
  print('Semaphore design: item_ready counts queue data, Semaphore(1) works as mutex')

  config = ClimateConfig()

  led_D13 = Pins(D13_PIN)
  rgb_led_D3 = RGBLed(D3_PIN, 4)
  rgb_led_D5 = RGBLed(D5_PIN, 4)
  rgb_led_D7 = RGBLed(D7_PIN, 4)
  lcd1602 = LCD1602()
  dht20 = DHT20()

  io_mutex = Semaphore(1)
  actuator_mutex = Semaphore(1)

  display_queue = RTOSQueue()
  heater_queue = RTOSQueue()
  cooler_queue = RTOSQueue()
  humidifier_queue = RTOSQueue()

  create_task(task_LED_Blinky(led_D13))
  create_task(task_Read_Temperature(dht20, [display_queue, heater_queue, cooler_queue, humidifier_queue], config))
  create_task(task_LCD_Display(display_queue, lcd1602, io_mutex))
  create_task(task_Heater(heater_queue, rgb_led_D3, actuator_mutex, io_mutex, config))
  create_task(task_Cooler(cooler_queue, rgb_led_D5, actuator_mutex, io_mutex, config))
  create_task(task_Humidifier(humidifier_queue, rgb_led_D7, actuator_mutex, io_mutex, config))


async def main():
  await setup()
  while True:
    await asleep_ms(100)


run_loop(main())
