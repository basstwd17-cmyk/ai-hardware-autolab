// GPIO-SPI CODE0..CODE3 register bank; no DAC, SMA, PRU, or status outputs.
//
// Mode 0, 24-bit frame, MSB first:
//   write: 0b000000aa, code[13:8], code[7:0]
//   read:  0b100000aa, 0x00,       0x00
// Read data is returned during bytes 2 and 3 as 00|CODE[13:0].
module spi_gpio_code_regs (
    input  wire spi_sclk,
    input  wire spi_mosi,
    input  wire spi_cs_n,
    output wire spi_miso
);
    reg [13:0] code0;
    reg [13:0] code1;
    reg [13:0] code2;
    reg [13:0] code3;
    reg [23:0] rx_shift;
    reg [15:0] tx_word;
    reg [4:0] bit_count;

    initial begin
        code0 = 14'h0000;
        code1 = 14'h1555;
        code2 = 14'h2AAA;
        code3 = 14'h3FFF;
        rx_shift = 24'h000000;
        tx_word = 16'h0000;
        bit_count = 5'd0;
    end

    // Chip-select high is the asynchronous frame boundary.  During a frame,
    // the master samples MISO on rising edges; tx_word is advanced immediately
    // after each prior rising edge, giving the next bit a full half-cycle.
    always @(posedge spi_sclk or posedge spi_cs_n) begin
        if (spi_cs_n) begin
            rx_shift <= 24'h000000;
            tx_word <= 16'h0000;
            bit_count <= 5'd0;
        end else begin
            rx_shift <= {rx_shift[22:0], spi_mosi};
            if (bit_count == 5'd7) begin
                case ({rx_shift[0], spi_mosi})
                    2'b00: tx_word <= {2'b00, code0};
                    2'b01: tx_word <= {2'b00, code1};
                    2'b10: tx_word <= {2'b00, code2};
                    default: tx_word <= {2'b00, code3};
                endcase
            end
            if (bit_count == 5'd23) begin
                if (!rx_shift[22] && rx_shift[21:17] == 5'b00000) begin
                    case (rx_shift[16:15])
                        2'b00: code0 <= {rx_shift[12:0], spi_mosi};
                        2'b01: code1 <= {rx_shift[12:0], spi_mosi};
                        2'b10: code2 <= {rx_shift[12:0], spi_mosi};
                        default: code3 <= {rx_shift[12:0], spi_mosi};
                    endcase
                end
                bit_count <= 5'd0;
            end else begin
                bit_count <= bit_count + 1'b1;
            end
        end
    end

    assign spi_miso = (!spi_cs_n && bit_count >= 5'd8 && bit_count < 5'd24)
                    ? tx_word[23 - bit_count] : 1'b0;
endmodule
