#pragma once
#include <Arduino.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

// 한 줄은 '요청번호 명령 인수'입니다. 줄 끝은 Enter(\n)입니다.
// 핀 번호를 외부에서 받지 않고, 배선 명세에 등록된 기능만 허용합니다.
void snapshot(unsigned int request);
bool command(unsigned int request, const char *name, char *args);
void safeStop();
char inputLine[80];
byte inputLength = 0;
bool discardLine = false;
unsigned long lastContact = 0;

void errorReply(unsigned int request, const __FlashStringHelper *code) {
  Serial.print(F("{\"v\":1,\"type\":\"error\",\"request\":"));
  Serial.print(request); Serial.print(F(",\"code\":\""));
  Serial.print(code); Serial.println(F("\"}"));
}
void applied(unsigned int request) {
  Serial.print(F("{\"v\":1,\"type\":\"ack\",\"request\":"));
  Serial.print(request); Serial.println(F(",\"applied\":true}"));
}
bool integer(const char *text, long low, long high, long &value) {
  if (!text || !*text) return false;
  char *end; errno=0; value = strtol(text, &end, 10);
  return errno!=ERANGE && *end == '\0' && value >= low && value <= high;
}
bool integers(char *text, byte count, long low, long high, long *values) {
  char *save=NULL; char *token=strtok_r(text," ",&save);
  for(byte i=0;i<count;i++) {
    if(!token || !integer(token,low,high,values[i])) return false;
    token=strtok_r(NULL," ",&save);
  }
  return token==NULL;
}
void stateStart(unsigned int request) {
  Serial.print(F("{\"v\":1,\"type\":\"state\",\"profile\":\""));
  Serial.print(PROFILE_ID); Serial.print(F("\",\"request\":"));
  Serial.print(request); Serial.print(F(",\"ms\":")); Serial.print(millis());
}
void dispatch() {
  char *space = strchr(inputLine, ' ');
  if (!space) { errorReply(0, F("format")); return; }
  *space = '\0'; long request;
  if (!integer(inputLine, 1, 65535, request)) { errorReply(0, F("request")); return; }
  char *name = space + 1;
  char *args = strchr(name, ' ');
  if (args) { *args = '\0'; args++; }
  else args = name + strlen(name);
  if (!strcmp(name, "STATUS") && !*args) { lastContact=millis(); snapshot(request); return; }
  if (!strcmp(name, "PING") && !*args) { lastContact=millis(); applied(request); return; }
  if (!strcmp(name, "STOP") && !*args) { lastContact=millis(); safeStop(); applied(request); snapshot(request); return; }
  // 각 프로파일의 명령 함수는 잘못된 인수나 지원하지 않는 명령을 거절합니다.
  if (command(request, name, args)) lastContact = millis();
}
void readCommands() {
  byte budget = 64; // 한 번에 읽는 양을 제한해 센서·모터 갱신을 막지 않습니다.
  while (budget-- && Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (discardLine) errorReply(0, F("line_too_long"));
      else if (inputLength) { inputLine[inputLength]='\0'; dispatch(); }
      inputLength=0; discardLine=false;
    } else if (!discardLine) {
      if (inputLength < sizeof(inputLine)-1) inputLine[inputLength++]=c;
      else { discardLine=true; inputLength=0; }
    }
  }
}
