// P03_PIN: 강의 P03 배선 그대로. 키패드와 빨강·노랑·초록 LED.
// 기준 P03 코드에서 바꾼 점 두 가지:
//  1) 키를 누를 때마다 순번(seq)이 붙은 key 메시지를 보냅니다. 같은 키를 연달아 눌러도 구분됩니다.
//  2) 0~3 키로 LED가 바뀌던 기능을 껐습니다. LED는 SIGNAL 명령으로만 바뀝니다.
// PIN 비교는 PC의 연결 프로그램이 합니다. 보드는 PIN을 모릅니다.
#include "pins.h"
#include "protocol.h"
#define FIRMWARE_ID "pin1"
const byte rows[4]={PIN_ROW1,PIN_ROW2,PIN_ROW3,PIN_ROW4};
const byte cols[4]={PIN_COL1,PIN_COL2,PIN_COL3,PIN_COL4};
const char keys[4][4]={{'1','2','3','A'},{'4','5','6','B'},{'7','8','9','C'},{'*','0','#','D'}};
byte signalState=0;
char lastKey='-', previous='-', candidate='-';
unsigned int keySeq=0;
unsigned long changedAt=0,lastScan=0,lastState=0;
char scanKey() {
  char found='-'; byte count=0;
  for(byte r=0;r<4;r++) {
    digitalWrite(rows[r],LOW); pinMode(rows[r],OUTPUT);
    for(byte c=0;c<4;c++) if(digitalRead(cols[c])==LOW) {found=keys[r][c];count++;}
    pinMode(rows[r],INPUT); // 선택하지 않은 행은 출력 HIGH로 구동하지 않습니다.
  }
  return count>1?'!':found; // 다중 입력은 단일 키로 확정하지 않습니다.
}
void setSignal(byte value) {
  signalState=value;
  digitalWrite(PIN_RED,value==1?HIGH:LOW);
  digitalWrite(PIN_YELLOW,value==2?HIGH:LOW);
  digitalWrite(PIN_GREEN,value==3?HIGH:LOW);
}
void safeStop() {setSignal(0);}
void snapshot(unsigned int request) {
  stateStart(request);
  Serial.print(F(",\"fw\":\"")); Serial.print(F(FIRMWARE_ID));
  Serial.print(F("\",\"last_key\":\"")); Serial.print(lastKey);
  Serial.print(F("\",\"key_seq\":")); Serial.print(keySeq);
  Serial.print(F(",\"signal\":")); Serial.print(signalState); Serial.println('}');
}
void keyEvent(char key) {
  keySeq++; // 65535 다음은 0으로 돌아갑니다. PC는 값이 바뀌었는지만 봅니다.
  Serial.print(F("{\"v\":1,\"type\":\"key\",\"seq\":")); Serial.print(keySeq);
  Serial.print(F(",\"key\":\"")); Serial.print(key); Serial.println(F("\"}"));
}
bool command(unsigned int request,const char *name,char *args) {
  long value;
  if(!strcmp(name,"SIGNAL") && integer(args,0,3,value)) {
    setSignal(value);applied(request);return true;
  }
  errorReply(request,F("unsupported_or_range"));return false;
}
void setup() {
  Serial.begin(115200);
  for(byte i=0;i<4;i++) {pinMode(rows[i],INPUT);pinMode(cols[i],INPUT_PULLUP);}
  pinMode(PIN_RED,OUTPUT);pinMode(PIN_YELLOW,OUTPUT);pinMode(PIN_GREEN,OUTPUT);safeStop();
  lastContact=millis();
}
void loop() {
  readCommands();unsigned long now=millis();
  if(now-lastScan>=10) {
    lastScan=now;char raw=scanKey();
    if(raw!=candidate) {candidate=raw;changedAt=now;}
    if(now-changedAt>=30 && previous!=candidate) {
      previous=candidate;
      if(previous!='-' && previous!='!') {
        lastKey=previous;
        keyEvent(previous);
      }
    }
  }
  if(now-lastState>=1000) {lastState=now;snapshot(0);}
}
