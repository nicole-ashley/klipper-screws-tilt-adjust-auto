#include <AFMotor.h> 
 
AF_DCMotor motor1(1, MOTOR12_1KHZ); 
AF_DCMotor motor2(2, MOTOR12_1KHZ); 
AF_DCMotor motor3(3, MOTOR34_1KHZ); 
AF_DCMotor motor4(4, MOTOR34_1KHZ);

AF_DCMotor motors[4] = {motor3, motor4, motor1, motor2};

// Stop a motor if Klipper hasn't told it to keep running for this long, so
// a crash or a dropped USB connection can't wind a screw out of the bed.
// Klipper resends the command every 2 seconds while a motor turns.
const unsigned long MOTOR_TIMEOUT_MS = 5000;

int runningMotor = 0;  // as sent by Klipper: negative is backward, 0 is none
unsigned long lastCommandAt = 0;

void stopMotors() {
  for (int i = 0; i < 4; i++) {
    Serial.print("Stop ");
    Serial.println(i);
    motors[i].run(RELEASE);
  }
  runningMotor = 0;
}

void runMotor(int index, bool forward) {
  Serial.print("Start ");
  Serial.print(index);
  Serial.println(forward ? " forward" : " backward");
  motors[index].run(forward ? FORWARD : BACKWARD);
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(5);

  for (int i = 0; i < 4; i++) {
    motors[i].setSpeed(255);
  }
  
  stopMotors();
}

void loop() {
  if (Serial.available() > 0) {
    int command = Serial.parseInt();
    lastCommandAt = millis();

    // The same command again just means keep going
    if (command == runningMotor && command != 0) {
      return;
    }

    stopMotors();
    int motor = abs(command);
    if (motor > 0 && motor <= 4) {
      runMotor(motor - 1, command > 0);
      runningMotor = command;
    }
  }

  if (runningMotor != 0 && millis() - lastCommandAt > MOTOR_TIMEOUT_MS) {
    Serial.println("Timed out waiting for Klipper");
    stopMotors();
  }
}