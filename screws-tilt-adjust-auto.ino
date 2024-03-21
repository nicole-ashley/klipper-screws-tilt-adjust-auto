#include <AFMotor.h> 
 
AF_DCMotor motor1(1, MOTOR12_1KHZ); 
AF_DCMotor motor2(2, MOTOR12_1KHZ); 
AF_DCMotor motor3(3, MOTOR34_1KHZ); 
AF_DCMotor motor4(4, MOTOR34_1KHZ);

AF_DCMotor motors[4] = {motor3, motor4, motor1, motor2};

void stopMotors() {
  for (int i = 0; i < 4; i++) {
    Serial.print("Stop ");
    Serial.println(i);
    motors[i].run(RELEASE);
  }
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
    stopMotors();

    int motor = Serial.parseInt();
    bool forward = motor > 0;
    motor = abs(motor);

    if (motor > 0 && motor <= 4) {
      runMotor(motor - 1, forward);
    }
  }
}