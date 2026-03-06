#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "sdkconfig.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_wifi_types.h"  // Added for CSI data types
#include "esp_timer.h"

// subcarrier sayısını arttırmak için b/g/n ayarı yapmam lazım!!! unutma

static const char *TAG = "csi_example";

// WiFi credentials
#define WIFI_SSID "beemo"
#define WIFI_PASS "12345678"

// FreeRTOS task handles
static TaskHandle_t wifi_task_handle = NULL;
static TaskHandle_t csi_task_handle = NULL;

// FreeRTOS queue for CSI data
#define CSI_QUEUE_SIZE 10
static QueueHandle_t csi_queue = NULL;

// FreeRTOS semaphore for WiFi initialization
static SemaphoreHandle_t wifi_init_semaphore = NULL;

// Structure to hold CSI data for queue
typedef struct {
    wifi_csi_info_t csi_info;
    int64_t timestamp;
} csi_data_t;

// CSI callback function - this gets called every time CSI data is received
//This is like a "listener" function - every time the ESP32 receives WiFi packets with CSI data, this function automatically gets called.
/* ctx (context): This is a generic pointer that lets you pass extra data to your callback. 
    Think of it like a "note" you can attach. We set it to NULL because we don't need it, 
    but you could pass a structure with your own data. */
static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info) {
    // Create CSI data structure
    csi_data_t csi_data = {
        .csi_info = *info,
        .timestamp = esp_timer_get_time()
    };
    
    // Send to queue (non-blocking)
    if (csi_queue != NULL) {
        xQueueSend(csi_queue, &csi_data, 0);
    }
}

// Task to process CSI data
static void csi_processing_task(void *pvParameters) {
    csi_data_t csi_data;
    
    ESP_LOGI(TAG, "CSI processing task started");
    
    while (1) {
        // Wait for CSI data from queue
        if (xQueueReceive(csi_queue, &csi_data, portMAX_DELAY)) {
            // Print CSI header info as JSON for easy Python parsing
            // Format must match Python CSV fieldnames exactly!
            printf("CSI_START{");
            printf("\"rssi\":%d,", csi_data.csi_info.rx_ctrl.rssi);
            printf("\"rate\":%d,", csi_data.csi_info.rx_ctrl.rate);
            printf("\"channel\":%d,", csi_data.csi_info.rx_ctrl.channel);
            printf("\"bandwidth\":%d,", csi_data.csi_info.rx_ctrl.cwb);
            printf("\"data_length\":%d,", csi_data.csi_info.len);  // Changed from "len" to "data_length"
            printf("\"esp_timestamp\":%lld,", csi_data.timestamp); // Changed from "timestamp" to "esp_timestamp"
            
            // The actual CSI data is in info->buf
            // Output ALL CSI data as comma-separated values
            printf("\"csi_data\":[");
            if (csi_data.csi_info.buf && csi_data.csi_info.len > 0) {
                int8_t *csi_raw = (int8_t *)csi_data.csi_info.buf;
                
                for (int i = 0; i < csi_data.csi_info.len; i++) {
                    printf("%d", csi_raw[i]);
                    if (i < csi_data.csi_info.len - 1) {
                        printf(",");
                    }
                }
            }
            printf("]}CSI_END\n");
            
            ESP_LOGD(TAG, "CSI packet processed - RSSI: %d, Length: %d", 
                    csi_data.csi_info.rx_ctrl.rssi, csi_data.csi_info.len);
        }
    }
}


static void event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data){
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        // WiFi started, now try to connect
        esp_wifi_connect();
        ESP_LOGI(TAG, "WiFi started, trying to connect to '%s'...", WIFI_SSID);
    } 
    else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        // Lost connection, try to reconnect with a slight delay
        wifi_event_sta_disconnected_t* disconnected = (wifi_event_sta_disconnected_t*) event_data;
        ESP_LOGI(TAG, "Disconnected from WiFi (reason: %d), retrying in 3 seconds...", disconnected->reason);
        
        // Wait 3 seconds before reconnecting to avoid rapid reconnect loops
        vTaskDelay(3000 / portTICK_PERIOD_MS);
        esp_wifi_connect();
    } 
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        // Successfully connected and got an IP address!
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "Connected! IP Address: " IPSTR, IP2STR(&event->ip_info.ip));
        
        // Signal that WiFi is initialized
        if (wifi_init_semaphore != NULL) {
            xSemaphoreGive(wifi_init_semaphore);
        }
    }
}

// Task to handle WiFi initialization and CSI setup
static void wifi_init_task(void *pvParameters) {
    ESP_LOGI(TAG, "WiFi initialization task started");
    
    /** INITIALIZE ALL THE THINGS **/
    //Initialize NVS
    esp_err_t ret = nvs_flash_init(); //Creates storage space for WiFi settings
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    //Initialize the TCP/IP stack
    ESP_ERROR_CHECK(esp_netif_init()); //Sets up internet protocols

    //initialize default esp event loop
    ESP_ERROR_CHECK(esp_event_loop_create_default()); //creates a message system so different parts can talk to each other

    //Create a WiFi station interface
    esp_netif_create_default_wifi_sta(); //Creates a WiFi client (STAtion)
    
    //Initialize WiFi with default configuration
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg)); //Turns on the WiFi chip inside the ESP32

    /** EVENT LOOP CRAZINESS **/
    //Register our event handler to listen for WiFi events
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL)); // This is a function pointer!
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL));
    //esp_event_handler_register() - Tell the ESP32 "when something WiFi-related happens, call my function"
    //The event_handler function responds to WiFi events like "started", "connected", "disconnected"

    //Configure WiFi settings
    wifi_config_t wifi_config = {};  // Zero out the entire structure
    memset(&wifi_config, 0, sizeof(wifi_config_t));
    
    // Properly copy SSID and password and null-terminate
    size_t ssid_len = strlen(WIFI_SSID);
    size_t pass_len = strlen(WIFI_PASS);
    
    memcpy(wifi_config.sta.ssid, WIFI_SSID, ssid_len);
    wifi_config.sta.ssid[ssid_len] = '\0';
    
    memcpy(wifi_config.sta.password, WIFI_PASS, pass_len);
    wifi_config.sta.password[pass_len] = '\0';
    
    // Try WPA2 first, but allow fallback to other auth modes
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    //set the wifi controller to be a station
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    //Give the ESP32 your WiFi credentials
    ESP_LOGI(TAG, "Setting WiFi credentials - SSID: '%s', Password: '%s'", WIFI_SSID, WIFI_PASS);
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    
    ESP_LOGI(TAG, "WiFi config set successfully");

    //Actually start trying to connect
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "WiFi initialization finished!");

    /** NOW WE WAIT FOR THE WIFI TO CONNECT **/

    // Wait for WiFi connection
    if (wifi_init_semaphore != NULL) {
        xSemaphoreTake(wifi_init_semaphore, portMAX_DELAY);
    }

    // NOW ENABLE CSI AFTER CONNECTION!
    ESP_LOGI(TAG, "Enabling CSI data collection...");
        
    // Step 1: Set up CSI configuration
    wifi_csi_config_t csi_config = {
        .lltf_en = true,        // Enable Long Training Field
        .htltf_en = true,       // Enable HT Long Training Field  
        .stbc_htltf2_en = true,        // Enable Space-Time Block Coding
        .ltf_merge_en = true,   // Enable LTF merging
        .channel_filter_en = false, // Disable channel filter for now
        .manu_scale = 0,        // Manual scaling (0 = auto)
    };
    
    // Step 2: Apply the CSI configuration
    ret = esp_wifi_set_csi_config(&csi_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set CSI config: %s (0x%x)", esp_err_to_name(ret), ret);
        ESP_LOGE(TAG, "CSI might not be enabled in menuconfig!");
        ESP_LOGE(TAG, "Check: Component config -> Wi-Fi -> Enable CSI");
        return;
    } else {
        ESP_LOGI(TAG, "CSI config set successfully!");
    }
    
    // Step 3: Register our callback function
    // YES this is the way espressif intended. You create your own callback function, and pass it to esp_wifi_set_csi_rx_cb()
    ret = esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set CSI callback: %s", esp_err_to_name(ret));
        return;
    }
    
    // Step 4: Enable CSI data collection
    ret = esp_wifi_set_csi(true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable CSI: %s", esp_err_to_name(ret));
        return;
    }
    
    ESP_LOGI(TAG, "CSI collection enabled successfully!");

    // Task is done, delete it
    vTaskDelete(NULL);
}

void app_main(void) {
    // vTaskStartScheduler(); is not needed in ESP-IDF. ESP-IDF starts the scheduler automatically after app_main() returns.
    // Initialize and start WiFi
    //wifi_init_sta();
    
    // Create semaphore for WiFi initialization
    wifi_init_semaphore = xSemaphoreCreateBinary();
    
    // Create queue for CSI data
    csi_queue = xQueueCreate(CSI_QUEUE_SIZE, sizeof(csi_data_t));
    
    if (wifi_init_semaphore == NULL || csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create FreeRTOS primitives");
        return;
    }

    //printf("ssid is: %s\n", CONFIG_SSID);
    //printf("password is: %s\n", CONFIG_PASSWORD);

    // Create WiFi initialization task
    xTaskCreate(wifi_init_task,
                "wifi_init",
                4096,  // Stack size
                NULL,
                5,     // Priority
                &wifi_task_handle);

    // Create CSI processing task
    xTaskCreate(csi_processing_task,
                "csi_process",
                4096,  // Stack size
                NULL,
                3,     // Priority
                &csi_task_handle);

    ESP_LOGI(TAG, "Tasks created, system starting...");

    /*
    // Keep the program running - otherwise watchdog gets angryyyy
    while(1) {
        ESP_LOGI(TAG, "Hello from main loop");
        vTaskDelay(4000 / portTICK_PERIOD_MS); // Wait 1 second
    */
}
